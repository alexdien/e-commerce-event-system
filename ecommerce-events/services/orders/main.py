import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager

from aiokafka import AIOKafkaProducer
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:29092")

producer: AIOKafkaProducer | None = None

class OrderRequest(BaseModel):
    product_id: str
    quantity: int
    customer_email: str

class OrderResponse(BaseModel):
    order_id: str
    status: str

# Kafka producer lifecycle
@asynccontextmanager
async def lifespan(app: FastAPI):
    global producer
    # Retry loop - Kafka may not be ready yet
    for attempt in range(10):
        try:
            producer = AIOKafkaProducer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_serializer=lambda v: json.dumps(v).encode(),
            )
            await producer.start()
            print("Kafka producer connected")
            break
        except Exception as e:
            print(f"Kafka not ready (attempt {attempt + 1}/10): {e}")
            await asyncio.sleep(3)
    else:
        raise RuntimeError("Could not connect to Kafka after 10 attempts")
    
    yield

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
        "customer_email": order.customer_email
    }

    await producer.send_and_wait("order.created", value=event)
    print(f"Published order.created: {order_id}")

    return OrderResponse(order_id=order_id, status="pending")