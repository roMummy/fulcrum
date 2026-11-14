"""合流已部署的好友检测迁移与主线群唯一约束迁移。"""


revision = "20261113_0040"
down_revision = ("20261112_0039", "20261113_0038")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 两个父分支各自维护表结构，合流只统一版本，不重复建表或改写已有检测记录。
    pass


def downgrade() -> None:
    pass
