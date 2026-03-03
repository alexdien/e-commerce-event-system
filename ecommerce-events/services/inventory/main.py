import asyncio
import json
import os
from contextlib import asynccontextmanager

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from fastapi import FastAPI

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")

# fake inventory
stock = {
    "TAYLORMADE": 50,
    "CALLAWAY": 10,
    "TITLEIST": 0
}

consumer: AIOKafkaConsumer | None = None
producer: AIOKafkaConsumer | None = None

async def consume_orders():
    """Background task that listens for orders.created.events"""
    async for msg in consumer:
        event = msg.value
        order_id = event["order_id"]
        product_id = event["product_id"]
        quantity = event["quantity"]

        print(f"Received order.created: {order_id}")

        available = stock.get(product_id, 0)

        if available >= quantity:
            stock[product_id] -= quantity
            result_event = {
                "order_id": order_id,
                "product_id": product_id,
                "quantity": quantity,
                "customer_email": event["customer_email"],
                "remaining_stock": stock[product_id]
            }
            await producer.send_and_wait("inventory.reserved", value = result_event)
            print(f"inventory.reserved (remaining: {stock[product_id]})")
        else:
            fail_event = {
                "order_id": order_id,
                "product_id": product_id,
                "requested": quantity,
                "available": available,
                "customer_email": event["customer_email"]
            }
            await producer.send_and_wait("inventory.failed", value = fail_event)
            print(f"inventory.failed (wanted {quantity}, but have {available})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global consumer, producer

    for attempt in range(10):
        try:
            consumer = AIOKafkaConsumer(
                "order.created",
                bootstrap_servers=KAFKA_BOOTSTRAP,
                group_id="inventory.group",
                value_deserializer=lambda v: json.loads(v.decode())
            )
            producer = AIOKafkaConsumer(
                bootstrap_servers=KAFKA_BOOTSTRAP,
                value_deserializer=lambda v: json.dumps(v).encode()
            )
            await consumer.start()
            await producer.start()
            print("Inventory consumer and producer connected")
            break
        except Exception as e:
            print(f"Kafka not ready (attempt {attempt + 1}/10: {e})")
            await asyncio.sleep(3)
    else:
        raise RuntimeError("Could not connect to Kafka after 10 attempts")
    
    task = asyncio.create_task(consume_orders())
    yield
    task.cancel()
    await consumer.stop()
    await producer.stop()

app = FastAPI(title="Inventory Service", lifespan=lifespan)

# endpoints
@app.get("/health")
async def health():
    return {"status": "ok", "service": "inventory"}

@app.get("/stock")
async def get_stock():
    return stock