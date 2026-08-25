"""L1 只读连接器（§4 数据接入双轨）。

设计取向：**框架层强制只读**，不靠每个连接器实现者自觉。
写动词在 base.execute() 就被拒绝，具体连接器根本没有写入通道。

凭据边界（§13.3）：连接器只持有 CredentialRef，明文密钥永不进
上下文、日志与任何序列化结果。
"""

from .base import (  # noqa: F401
    Connector,
    ConnectorSpec,
    CredentialRef,
    PullQuota,
    PullResult,
    build_connector,
    get_spec,
    list_specs,
    register,
)

from . import presets  # noqa: E402,F401  导入即注册预置连接器
