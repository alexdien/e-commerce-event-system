# Mini E-Commerce Event System — Lab Guide

## A hands-on project to learn FastAPI, Docker, Kubernetes, and Kafka

**Estimated time:** 3–4 hours
**Difficulty:** Intermediate
**Prerequisites:** Python 3.10+, basic REST API knowledge, Docker Desktop installed, VS Code

---

## What You're Building

An event-driven microservices system with three services that communicate through Apache Kafka:

```
┌──────────────┐       ┌──────────────────┐       ┌────────────────────┐
│              │       │                  │       │                    │
│  Orders API  │──────▶│  Apache Kafka    │◀──────│  Inventory Service │
│  (FastAPI)   │       │  (Event Broker)  │       │  (FastAPI)         │
│              │       │                  │       │                    │
└──────────────┘       └──────────────────┘       └────────────────────┘
                              │
                              ▼
                       ┌────────────────────┐
                       │                    │
                       │  Notifications Svc │
                       │  (FastAPI)         │
                       │                    │
                       └────────────────────┘
```

**Event flow:**
1. A customer places an order via the **Orders** service
2. Orders publishes an `order.created` event to Kafka
3. **Inventory** consumes the event, reserves stock, and publishes `inventory.reserved` or `inventory.failed`
4. **Notifications** consumes both event types and logs a confirmation or failure message

---

## Project Structure

Create this folder structure in VS Code. Each piece is explained in the phases below.

```
ecommerce-events/
├── docker-compose.yml
├── k8s/
│   ├── namespace.yml
│   ├── kafka.yml
│   ├── zookeeper.yml
│   ├── orders.yml
│   ├── inventory.yml
│   └── notifications.yml
│
├── services/
│   ├── orders/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── main.py
│   │
│   ├── inventory/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── main.py
│   │
│   └── notifications/
│       ├── Dockerfile
│       ├── requirements.txt
│       └── main.py
```

---

## Phase 1 — FastAPI Services (60–75 min)

### 1.1 Orders Service

This is the entry point. It exposes a POST endpoint to create orders and publishes events to Kafka.

**`services/orders/requirements.txt`**
```
fastapi==0.111.0
uvicorn==0.30.1
aiokafka==0.10.0
pydantic==2.7.4
```

**`services/orders/main.py`**
```python
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager

from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")

producer: AIOKafkaProducer | None = None


class OrderRequest(BaseModel):
    product_id: str
    quantity: int
    customer_email: str


class OrderResponse(BaseModel):
    order_id: str
    status: str


# ── Kafka producer lifecycle ────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global producer
    # Retry loop — Kafka may not be ready yet
    for attempt in range(10):
        try:
            producer = AIOKafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode(),
            )
            await producer.start()
            print("✓ Kafka producer connected")
            break
        except Exception as e:
            print(f"  Kafka not ready (attempt {attempt + 1}/10): {e}")
            await asyncio.sleep(3)
    else:
        raise RuntimeError("Could not connect to Kafka after 10 attempts")

    yield  # app runs here

    await producer.stop()


app = FastAPI(title="Orders Service", lifespan=lifespan)


# ── Health check ────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "service": "orders"}


# ── Create an order ─────────────────────────────────────────────
@app.post("/orders", response_model=OrderResponse)
async def create_order(order: OrderRequest):
    order_id = str(uuid.uuid4())

    event = {
        "order_id": order_id,
        "product_id": order.product_id,
        "quantity": order.quantity,
        "customer_email": order.customer_email,
    }

    await producer.send_and_wait("order.created", value=event)
    print(f"→ Published order.created: {order_id}")

    return OrderResponse(order_id=order_id, status="pending")
```

**What to notice:**
- `lifespan` is FastAPI's modern way to run startup/shutdown logic (replaces `@app.on_event`)
- The retry loop is essential — in containers, Kafka often starts slower than your app
- `aiokafka` is fully async, so it fits naturally with FastAPI's async endpoints
- The service is stateless; the "order" only exists as a Kafka event

### 1.2 Inventory Service

Consumes `order.created`, checks a fake in-memory stock dict, and publishes the result.

**`services/inventory/requirements.txt`**
```
fastapi==0.111.0
uvicorn==0.30.1
aiokafka==0.10.0
```

**`services/inventory/main.py`**
```python
import asyncio
import json
import os
from contextlib import asynccontextmanager

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from fastapi import FastAPI

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")

# ── Fake inventory database ─────────────────────────────────────
stock = {
    "WIDGET-001": 50,
    "WIDGET-002": 10,
    "GADGET-001": 0,   # intentionally out of stock for testing
}

consumer: AIOKafkaConsumer | None = None
producer: AIOKafkaProducer | None = None


async def consume_orders():
    """Background task that listens for order.created events."""
    async for msg in consumer:
        event = msg.value
        order_id = event["order_id"]
        product_id = event["product_id"]
        quantity = event["quantity"]

        print(f"← Received order.created: {order_id}")

        available = stock.get(product_id, 0)

        if available >= quantity:
            stock[product_id] -= quantity
            result_event = {
                "order_id": order_id,
                "product_id": product_id,
                "quantity": quantity,
                "customer_email": event["customer_email"],
                "remaining_stock": stock[product_id],
            }
            await producer.send_and_wait("inventory.reserved", value=result_event)
            print(f"  → inventory.reserved (remaining: {stock[product_id]})")
        else:
            fail_event = {
                "order_id": order_id,
                "product_id": product_id,
                "requested": quantity,
                "available": available,
                "customer_email": event["customer_email"],
            }
            await producer.send_and_wait("inventory.failed", value=fail_event)
            print(f"  → inventory.failed (wanted {quantity}, have {available})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global consumer, producer

    for attempt in range(10):
        try:
            consumer = AIOKafkaConsumer(
                "order.created",
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id="inventory-group",
                value_deserializer=lambda v: json.loads(v.decode()),
            )
            producer = AIOKafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode(),
            )
            await consumer.start()
            await producer.start()
            print("✓ Inventory consumer + producer connected")
            break
        except Exception as e:
            print(f"  Kafka not ready (attempt {attempt + 1}/10): {e}")
            await asyncio.sleep(3)
    else:
        raise RuntimeError("Could not connect to Kafka after 10 attempts")

    task = asyncio.create_task(consume_orders())
    yield
    task.cancel()
    await consumer.stop()
    await producer.stop()


app = FastAPI(title="Inventory Service", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "inventory"}


@app.get("/stock")
async def get_stock():
    return stock
```

**What to notice:**
- This service is both a **consumer** and a **producer** — a very common pattern
- `group_id` ensures that if you scale to multiple replicas, each message is only processed once
- The `consume_orders` coroutine runs as a background task alongside the FastAPI server

### 1.3 Notifications Service

Consumes both `inventory.reserved` and `inventory.failed` and logs messages. In a real system this would send emails, SMS, or push notifications.

**`services/notifications/requirements.txt`**
```
fastapi==0.111.0
uvicorn==0.30.1
aiokafka==0.10.0
```

**`services/notifications/main.py`**
```python
import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer
from fastapi import FastAPI

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")

# ── In-memory notification log (for the GET endpoint) ──────────
notifications: list[dict] = []

consumer: AIOKafkaConsumer | None = None


async def consume_events():
    """Listen for inventory outcomes and 'send' notifications."""
    async for msg in consumer:
        event = msg.value
        topic = msg.topic
        order_id = event["order_id"]
        email = event["customer_email"]
        timestamp = datetime.now(timezone.utc).isoformat()

        if topic == "inventory.reserved":
            body = (
                f"Order {order_id}: your {event['quantity']}× "
                f"{event['product_id']} has been confirmed!"
            )
        else:
            body = (
                f"Order {order_id}: sorry, {event['product_id']} is out of "
                f"stock (requested {event['requested']}, "
                f"available {event['available']})"
            )

        notification = {
            "timestamp": timestamp,
            "to": email,
            "body": body,
            "topic": topic,
        }
        notifications.append(notification)
        print(f"📧 [{topic}] → {email}: {body}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global consumer

    for attempt in range(10):
        try:
            consumer = AIOKafkaConsumer(
                "inventory.reserved",
                "inventory.failed",
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id="notifications-group",
                value_deserializer=lambda v: json.loads(v.decode()),
            )
            await consumer.start()
            print("✓ Notifications consumer connected")
            break
        except Exception as e:
            print(f"  Kafka not ready (attempt {attempt + 1}/10): {e}")
            await asyncio.sleep(3)
    else:
        raise RuntimeError("Could not connect to Kafka after 10 attempts")

    task = asyncio.create_task(consume_events())
    yield
    task.cancel()
    await consumer.stop()


app = FastAPI(title="Notifications Service", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "notifications"}


@app.get("/notifications")
async def get_notifications():
    return {"count": len(notifications), "items": notifications[-50:]}
```

### 1.4 Test Locally (Optional Quick Check)

Before containerizing, you can spin up Kafka locally with Docker and run each service in a separate VS Code terminal:

```bash
# Terminal 1 — start Kafka (single-node via docker)
docker run -d --name kafka-local \
  -p 9092:9092 \
  -e KAFKA_CFG_NODE_ID=0 \
  -e KAFKA_CFG_PROCESS_ROLES=controller,broker \
  -e KAFKA_CFG_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093 \
  -e KAFKA_CFG_ADVERTISED_LISTENERS=PLAINTEXT://localhost:9092 \
  -e KAFKA_CFG_CONTROLLER_QUORUM_VOTERS=0@localhost:9093 \
  -e KAFKA_CFG_CONTROLLER_LISTENER_NAMES=CONTROLLER \
  -e KAFKA_CFG_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
  bitnami/kafka:3.7

# Terminal 2
cd services/orders && pip install -r requirements.txt && uvicorn main:app --port 8001

# Terminal 3
cd services/inventory && pip install -r requirements.txt && uvicorn main:app --port 8002

# Terminal 4
cd services/notifications && pip install -r requirements.txt && uvicorn main:app --port 8003
```

```bash
# Test it
curl -X POST http://localhost:8001/orders \
  -H "Content-Type: application/json" \
  -d '{"product_id":"WIDGET-001","quantity":2,"customer_email":"test@example.com"}'

# Check notifications
curl http://localhost:8003/notifications

# Check remaining stock
curl http://localhost:8002/stock
```

Clean up when done: `docker stop kafka-local && docker rm kafka-local`

---

## Phase 2 — Dockerize Everything (30–45 min)

### 2.1 Dockerfiles

All three services use the same Dockerfile pattern. Create one in each service folder.

**`services/orders/Dockerfile`** (same pattern for inventory and notifications)
```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Copy this file into `services/inventory/Dockerfile` and `services/notifications/Dockerfile` as well — they're identical.

### 2.2 Docker Compose

This file wires Kafka, Zookeeper, and all three services together on a shared Docker network.

**`docker-compose.yml`**
```yaml
version: "3.9"

services:
  # ── Kafka Infrastructure ──────────────────────────────────────
  zookeeper:
    image: bitnami/zookeeper:3.9
    environment:
      ALLOW_ANONYMOUS_LOGIN: "yes"
    ports:
      - "2181:2181"

  kafka:
    image: bitnami/kafka:3.7
    depends_on:
      - zookeeper
    ports:
      - "9092:9092"
    environment:
      KAFKA_CFG_ZOOKEEPER_CONNECT: zookeeper:2181
      KAFKA_CFG_LISTENERS: PLAINTEXT://:9092
      KAFKA_CFG_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092
      KAFKA_CFG_AUTO_CREATE_TOPICS_ENABLE: "true"

  # ── Application Services ──────────────────────────────────────
  orders:
    build: ./services/orders
    ports:
      - "8001:8000"
    environment:
      KAFKA_BOOTSTRAP: kafka:9092
    depends_on:
      - kafka

  inventory:
    build: ./services/inventory
    ports:
      - "8002:8000"
    environment:
      KAFKA_BOOTSTRAP: kafka:9092
    depends_on:
      - kafka

  notifications:
    build: ./services/notifications
    ports:
      - "8003:8000"
    environment:
      KAFKA_BOOTSTRAP: kafka:9092
    depends_on:
      - kafka
```

### 2.3 Build and Run

```bash
cd ecommerce-events

# Build all images and start everything
docker compose up --build

# In another terminal, test the full pipeline
curl -X POST http://localhost:8001/orders \
  -H "Content-Type: application/json" \
  -d '{"product_id":"WIDGET-001","quantity":3,"customer_email":"alice@example.com"}'

# Watch the docker compose terminal — you'll see:
#   orders        | → Published order.created: ...
#   inventory     | ← Received order.created: ...
#   inventory     |   → inventory.reserved (remaining: 47)
#   notifications | 📧 [inventory.reserved] → alice@example.com: ...

# Test the failure path
curl -X POST http://localhost:8001/orders \
  -H "Content-Type: application/json" \
  -d '{"product_id":"GADGET-001","quantity":1,"customer_email":"bob@example.com"}'

# Check the endpoints
curl http://localhost:8002/stock
curl http://localhost:8003/notifications
```

### 2.4 Key Docker Concepts to Observe

**While working through this phase, pay attention to:**

- **Layer caching**: The `COPY requirements.txt` + `RUN pip install` happens before `COPY .` — this means rebuilds after code changes skip the slow pip install step
- **Service networking**: Containers reference each other by service name (`kafka:9092`, not `localhost:9092`). Docker Compose creates a shared network automatically.
- **Port mapping**: `8001:8000` means host port 8001 maps to container port 8000. All containers run on port 8000 internally.
- **`depends_on` limitations**: This only waits for the container to *start*, not for Kafka to be *ready*. That's why the retry loops in the Python code are critical.

Clean up: `docker compose down`

---

## Phase 3 — Deploy on Minikube with Kubernetes (60–90 min)

### 3.1 Setup Minikube

```bash
# Install minikube if you haven't
# macOS:   brew install minikube
# Linux:   curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64 \
#            && sudo install minikube-linux-amd64 /usr/local/bin/minikube

# Start cluster
minikube start --memory=4096 --cpus=2

# IMPORTANT: point your docker CLI at minikube's internal Docker daemon
# so that image builds are available to Kubernetes without a registry
eval $(minikube docker-env)

# Rebuild images inside minikube's Docker
docker compose build
```

### 3.2 Kubernetes Manifests

**`k8s/namespace.yml`**
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: ecommerce
```

**`k8s/zookeeper.yml`**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: zookeeper
  namespace: ecommerce
spec:
  replicas: 1
  selector:
    matchLabels:
      app: zookeeper
  template:
    metadata:
      labels:
        app: zookeeper
    spec:
      containers:
        - name: zookeeper
          image: bitnami/zookeeper:3.9
          ports:
            - containerPort: 2181
          env:
            - name: ALLOW_ANONYMOUS_LOGIN
              value: "yes"
---
apiVersion: v1
kind: Service
metadata:
  name: zookeeper
  namespace: ecommerce
spec:
  selector:
    app: zookeeper
  ports:
    - port: 2181
      targetPort: 2181
```

**`k8s/kafka.yml`**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kafka
  namespace: ecommerce
spec:
  replicas: 1
  selector:
    matchLabels:
      app: kafka
  template:
    metadata:
      labels:
        app: kafka
    spec:
      containers:
        - name: kafka
          image: bitnami/kafka:3.7
          ports:
            - containerPort: 9092
          env:
            - name: KAFKA_CFG_ZOOKEEPER_CONNECT
              value: "zookeeper:2181"
            - name: KAFKA_CFG_LISTENERS
              value: "PLAINTEXT://:9092"
            - name: KAFKA_CFG_ADVERTISED_LISTENERS
              value: "PLAINTEXT://kafka:9092"
            - name: KAFKA_CFG_AUTO_CREATE_TOPICS_ENABLE
              value: "true"
---
apiVersion: v1
kind: Service
metadata:
  name: kafka
  namespace: ecommerce
spec:
  selector:
    app: kafka
  ports:
    - port: 9092
      targetPort: 9092
```

**`k8s/orders.yml`**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders
  namespace: ecommerce
spec:
  replicas: 1
  selector:
    matchLabels:
      app: orders
  template:
    metadata:
      labels:
        app: orders
    spec:
      containers:
        - name: orders
          image: ecommerce-events-orders:latest
          imagePullPolicy: Never       # use local image from minikube's Docker
          ports:
            - containerPort: 8000
          env:
            - name: KAFKA_BOOTSTRAP
              value: "kafka:9092"
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: orders
  namespace: ecommerce
spec:
  type: NodePort
  selector:
    app: orders
  ports:
    - port: 8000
      targetPort: 8000
      nodePort: 30001
```

**`k8s/inventory.yml`**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: inventory
  namespace: ecommerce
spec:
  replicas: 1
  selector:
    matchLabels:
      app: inventory
  template:
    metadata:
      labels:
        app: inventory
    spec:
      containers:
        - name: inventory
          image: ecommerce-events-inventory:latest
          imagePullPolicy: Never
          ports:
            - containerPort: 8000
          env:
            - name: KAFKA_BOOTSTRAP
              value: "kafka:9092"
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: inventory
  namespace: ecommerce
spec:
  type: NodePort
  selector:
    app: inventory
  ports:
    - port: 8000
      targetPort: 8000
      nodePort: 30002
```

**`k8s/notifications.yml`**
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: notifications
  namespace: ecommerce
spec:
  replicas: 1
  selector:
    matchLabels:
      app: notifications
  template:
    metadata:
      labels:
        app: notifications
    spec:
      containers:
        - name: notifications
          image: ecommerce-events-notifications:latest
          imagePullPolicy: Never
          ports:
            - containerPort: 8000
          env:
            - name: KAFKA_BOOTSTRAP
              value: "kafka:9092"
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 10
            periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: notifications
  namespace: ecommerce
spec:
  type: NodePort
  selector:
    app: notifications
  ports:
    - port: 8000
      targetPort: 8000
      nodePort: 30003
```

### 3.3 Deploy

```bash
# Apply everything in order
kubectl apply -f k8s/namespace.yml
kubectl apply -f k8s/zookeeper.yml
kubectl apply -f k8s/kafka.yml

# Wait for Kafka to be ready
kubectl -n ecommerce get pods -w
# (wait until kafka and zookeeper pods show 1/1 Running)

kubectl apply -f k8s/orders.yml
kubectl apply -f k8s/inventory.yml
kubectl apply -f k8s/notifications.yml

# Watch all pods come up
kubectl -n ecommerce get pods -w
```

### 3.4 Test on Minikube

```bash
# Get the minikube IP
MINIKUBE_IP=$(minikube ip)

# Place an order
curl -X POST http://$MINIKUBE_IP:30001/orders \
  -H "Content-Type: application/json" \
  -d '{"product_id":"WIDGET-002","quantity":5,"customer_email":"carol@example.com"}'

# Check stock
curl http://$MINIKUBE_IP:30002/stock

# Check notifications
curl http://$MINIKUBE_IP:30003/notifications

# View logs for the entire pipeline
kubectl -n ecommerce logs -l app=orders --tail=20
kubectl -n ecommerce logs -l app=inventory --tail=20
kubectl -n ecommerce logs -l app=notifications --tail=20
```

### 3.5 Key Kubernetes Concepts to Observe

Walk through these commands to see what Kubernetes is actually doing:

```bash
# See all resources in your namespace
kubectl -n ecommerce get all

# Describe a pod to see events (scheduling, pulling images, health checks)
kubectl -n ecommerce describe pod -l app=orders

# Watch readiness probes in action — the /health endpoint you wrote is real
kubectl -n ecommerce get endpoints

# Scale inventory to 2 replicas and observe Kafka consumer group rebalancing
kubectl -n ecommerce scale deployment/inventory --replicas=2
kubectl -n ecommerce get pods -w

# Kill a pod and watch Kubernetes restart it
kubectl -n ecommerce delete pod -l app=orders
kubectl -n ecommerce get pods -w
```

---

## Phase 4 — Stretch Goals (if you have time)

These are optional exercises that build on top of the core lab. Each adds one real-world concept.

### 4.1 Add a Dead Letter Queue

When inventory processing fails due to an unexpected error (not just "out of stock"), publish the event to a `dead-letter` topic instead of silently dropping it. Add a `try/except` around the event processing in the inventory consumer.

### 4.2 Add a Retry with Backoff

Modify the orders service to accept a webhook URL in the order request. Have the notifications service attempt an HTTP POST to that webhook (using `httpx`), retrying 3 times with exponential backoff on failure.

### 4.3 Add Prometheus Metrics

Install `prometheus-fastapi-instrumentator` in each service and expose a `/metrics` endpoint. Then deploy a Prometheus instance in minikube to scrape all three services.

### 4.4 Add a Simple API Gateway

Create an nginx reverse proxy (as another container) that routes `/api/orders/*` to the orders service, `/api/inventory/*` to inventory, and `/api/notifications/*` to notifications. Deploy it as a Kubernetes Ingress.

### 4.5 Persist Inventory with Redis

Replace the in-memory `stock` dict with a Redis backend. Add a Redis deployment to your `k8s/` folder and use `aioredis` in the inventory service.

---

## Troubleshooting Cheat Sheet

| Symptom | Likely Cause | Fix |
|---|---|---|
| `KafkaConnectionError` on startup | Kafka isn't ready yet | The retry loop should handle this; increase attempts if needed |
| Orders service returns 500 | Producer failed to connect | Check `docker compose logs kafka` or `kubectl -n ecommerce logs -l app=kafka` |
| Notifications never appear | Consumer group not receiving | Check `kubectl -n ecommerce logs -l app=notifications` for connection errors |
| `ImagePullBackOff` in k8s | Minikube can't find local image | Run `eval $(minikube docker-env)` then rebuild images |
| Pods stuck in `CrashLoopBackOff` | App crashing on startup | Check logs: `kubectl -n ecommerce logs <pod-name>` |
| `NodePort` not reachable | Minikube networking | Try `minikube service orders -n ecommerce --url` instead |

---

## Concepts Checklist

Check these off as you go. By the end of the lab you should understand each one.

**FastAPI:**
- [ ] Async request handlers with `async def`
- [ ] Pydantic models for request/response validation
- [ ] Lifespan events for startup/shutdown
- [ ] Background tasks running alongside the server

**Docker:**
- [ ] Multi-stage layer caching with `COPY requirements.txt` before `COPY .`
- [ ] Docker Compose service networking (services reference each other by name)
- [ ] Port mapping (host:container)
- [ ] Environment variable injection

**Kafka:**
- [ ] Topics as event channels (`order.created`, `inventory.reserved`, `inventory.failed`)
- [ ] Producers (fire-and-forget publishing)
- [ ] Consumers with consumer groups (each message processed once per group)
- [ ] Event-driven decoupling (services don't call each other directly)

**Kubernetes:**
- [ ] Deployments (desired state, self-healing restarts)
- [ ] Services (stable networking within the cluster)
- [ ] NodePort (exposing services outside the cluster)
- [ ] Health probes (readiness and liveness)
- [ ] Namespaces (resource isolation)
- [ ] Scaling replicas and observing consumer group rebalancing
- [ ] `imagePullPolicy: Never` for local development

---

## What to Put on Your Resume

After completing this lab, you can describe it as:

> **Event-Driven Microservices Platform** — Designed and built a 3-service e-commerce system (orders, inventory, notifications) using FastAPI and Apache Kafka for async event streaming. Containerized with Docker, orchestrated with Kubernetes on Minikube. Implemented consumer groups, health probes, retry logic, and service discovery.

Good luck — have fun building this.
