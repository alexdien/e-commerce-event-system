import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from aiokafka import AIOKafkaConsumer
from fastapi import FastAPI

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP", "localhost:9092")

#  In-memory notification log (for the GET endpoint)
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