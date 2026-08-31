"""开发用启动脚本：在 backend/ 目录下执行 `python run_server.py` 即可启动服务。

等价于命令行：uvicorn app.main:app --host 0.0.0.0 --port 8100
端口/模型等配置从项目根 .env 读取（见 app/config.py 的 Settings）。
"""
import uvicorn

from app.config import settings
from app.main import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port,
        # 开发时改为 True 可改代码自动重启（需要 uvloop/watchfiles，venv 里已装）
        reload=False,
    )
