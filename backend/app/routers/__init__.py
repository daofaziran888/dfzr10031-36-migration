"""业务模块路由汇总。

这里统一按别名导入再暴露 ROUTERS：模块名有可能和内置名撞车（某个业务模块就叫 dict、list
这种名字时），按名字直接 import 会把内置类型覆盖掉，函数注解在运行时求值就会报
'module' object is not subscriptable。
"""
from __future__ import annotations

from app.routers import metric_def as router_metric_def
from app.routers import dimension as router_dimension
from app.routers import dataset as router_dataset
from app.routers import experiment as router_experiment
from app.routers import query_builder as router_query_builder
from app.routers import dashboard as router_dashboard
from app.routers import chart as router_chart
from app.routers import data_service as router_data_service
from app.routers import alert as router_alert
from app.routers import user_group as router_user_group
from app.routers import snapshot as router_snapshot
from app.routers import scheduler as router_scheduler
from app.routers import cache as router_cache
from app.routers import pipeline as router_pipeline
from app.routers import model as router_model
from app.routers import data_mart as router_data_mart
from app.routers import export as router_export
from app.routers import audit as router_audit
from app.routers import audit_time_migration as router_audit_time_migration

ROUTERS = [router_metric_def, router_dimension, router_dataset, router_experiment, router_query_builder, router_dashboard, router_chart, router_data_service, router_alert, router_user_group, router_snapshot, router_scheduler, router_cache, router_pipeline, router_model, router_data_mart, router_export, router_audit, router_audit_time_migration]
