"""RQ Worker 入口：在 backend 目录执行 ../.venv/bin/python worker.py。"""
from redis import Redis
from rq import Worker

from app.config import settings

if __name__ == "__main__":
    Worker([settings.rq_queue_name], connection=Redis.from_url(settings.redis_url)).work()
