#!/usr/bin/env python3
"""
Celery启动脚本
启动Celery Worker和Beat调度器
"""

import os
import sys
import subprocess
import signal
import time
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def start_celery_worker():
    """启动Celery Worker"""
    print("🚀 Starting Celery Worker...")
    
    cmd = [
        "celery", "-A", "backend.core.celery_app", "worker",
        "--loglevel=info",
        "--concurrency=2",
        "--queues=processing,video,notification,maintenance",
        "--hostname=worker1@%h"
    ]
    
    try:
        process = subprocess.Popen(cmd, cwd=str(project_root))
        print(f"✅ Celery Worker started (PID: {process.pid})")
        return process
    except Exception as e:
        print(f"❌ Failed to start Celery Worker: {e}")
        return None

def start_celery_beat():
    """启动Celery Beat调度器"""
    print("⏰ Starting Celery Beat scheduler...")
    
    cmd = [
        "celery", "-A", "backend.core.celery_app", "beat",
        "--loglevel=info",
        "--schedule=/tmp/celerybeat-schedule",
        "--pidfile=/tmp/celerybeat.pid"
    ]
    
    try:
        process = subprocess.Popen(cmd, cwd=str(project_root))
        print(f"✅ Celery Beat started (PID: {process.pid})")
        return process
    except Exception as e:
        print(f"❌ Failed to start Celery Beat: {e}")
        return None

def start_flower():
    """启动Flower监控界面"""
    print("🌸 Starting Flower monitoring UI...")
    
    cmd = [
        "celery", "-A", "backend.core.celery_app", "flower",
        "--port=5555",
        "--loglevel=info"
    ]
    
    try:
        process = subprocess.Popen(cmd, cwd=str(project_root))
        print(f"✅ Flower started (PID: {process.pid})")
        print("🌐 Flower monitoring UI: http://localhost:5555")
        return process
    except Exception as e:
        print(f"❌ Failed to start Flower: {e}")
        return None

def signal_handler(signum, frame):
    """信号处理函数"""
    print("\n🛑 Stop signal received, shutting down services...")
    sys.exit(0)

def main():
    """主函数"""
    print("🎯 AutoClip Celery task queue launcher")
    print("=" * 50)
    
    # 设置信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 检查Redis连接
    try:
        import redis
        r = redis.Redis.from_url('redis://localhost:6379/0')
        r.ping()
        print("✅ Redis connection OK")
    except Exception as e:
        print(f"❌ Redis connection failed: {e}")
        print("Make sure the Redis service is running: redis-server")
        return
    
    # 启动服务
    processes = []
    
    # 启动Worker
    worker_process = start_celery_worker()
    if worker_process:
        processes.append(worker_process)
    
    # 启动Beat
    beat_process = start_celery_beat()
    if beat_process:
        processes.append(beat_process)
    
    # 启动Flower
    flower_process = start_flower()
    if flower_process:
        processes.append(flower_process)
    
    if not processes:
        print("❌ No services were started successfully")
        return
    
    print("\n🎉 All services started!")
    print("📊 Service status:")
    print("   - Celery Worker: processes tasks")
    print("   - Celery Beat: scheduled task dispatcher")
    print("   - Flower: task monitoring UI (http://localhost:5555)")
    print("\nPress Ctrl+C to stop all services")
    
    try:
        # 等待进程
        while True:
            time.sleep(1)
            # 检查进程是否还在运行
            for process in processes:
                if process.poll() is not None:
                    print(f"⚠️  Process {process.pid} has exited")
    except KeyboardInterrupt:
        print("\n🛑 Stopping services...")
    finally:
        # 停止所有进程
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                print(f"🛑 Process {process.pid} stopped")

if __name__ == "__main__":
    main() 