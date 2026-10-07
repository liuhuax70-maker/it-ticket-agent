"""离线任务 / DAG。

栈内没有引入 Airflow / Dagster，这些任务写成**可直接执行的 Python 模块**，
用 cron / K8s CronJob / CI 触发即可；后续若要接调度器，
只需在调度器里把这些 ``run()`` 包一层，业务逻辑不用改。
"""
