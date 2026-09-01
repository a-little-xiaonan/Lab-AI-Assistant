"""开发用启动脚本：在 backend/ 目录下执行 `python run_server.py` 即可启动服务。

等价于命令行：uvicorn app.main:app --host 0.0.0.0 --port 8100
端口/模型等配置从项目根 .env 读取（见 app/config.py 的 Settings）。
"""
import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run(
        # reload 模式必须传导入字符串，不能传已经创建好的 app 对象。
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        # 开发环境自动重载：修改 backend/app 下的代码后无需手动重启。
        reload=True,
        reload_dirs=["app"],
    )
