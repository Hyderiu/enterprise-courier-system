from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

_scheduler = None

def start_scheduler():
    from datetime import datetime

    global _scheduler
    if _scheduler: return
    _scheduler = BackgroundScheduler(
        timezone="Asia/Shanghai",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )
    from scheduler.jobs import (
        backfill_monthly_exports, cancel_stale_sf_city_orders, poll_tracking,
        run_monthly_exports,
        send_pending_approval_reminders, sync_crm_reference_data, sync_reference_data,
    )
    _scheduler.add_job(
        sync_reference_data,
        CronTrigger(day_of_week="mon", hour=9, minute=30),
        id="sync_reference_data_weekly_monday_0930",
        # CRM 基础资料和员工通讯录每周一 09:30 自动同步。服务启动时不补跑，
        # 避免重启或多实例恢复时额外消耗钉钉调用次数。
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        sync_crm_reference_data,
        CronTrigger(day_of_week="tue-sun", hour=9, minute=30),
        id="sync_crm_reference_daily_tuesday_sunday_0930",
        # 周一由上面的组合任务先同步 CRM 再同步员工；其余日期只同步 CRM，
        # 因而项目和部门仍然每天更新，而钉钉全体员工只在周一读取一次。
        misfire_grace_time=3600,
    )
    _scheduler.add_job(
        poll_tracking, IntervalTrigger(minutes=10), id="poll_tracking_10min",
        # 启动后立即刷新一次，之后每10分钟同步物流轨迹和实际费用。
        next_run_time=datetime.now(_scheduler.timezone),
        max_instances=1, coalesce=True,
    )
    _scheduler.add_job(
        cancel_stale_sf_city_orders,
        IntervalTrigger(minutes=1), id="sf_city_auto_cancel_1min",
        # 只处理超过配置等待时长的同城订单；取消前会重新查询真实状态，
        # 防止骑士刚接单但回调尚未入库时被误取消。
        max_instances=1, coalesce=True,
    )
    _scheduler.add_job(
        send_pending_approval_reminders,
        IntervalTrigger(minutes=1),
        id="pending_approval_reminders_1min",
        # 每分钟只检查本地数据库；真正发信仍严格遵循审批设置中的重复提醒间隔。
        # 每条记录保存上次提醒时间，服务重启后继续计时且不会重复轰炸。
        max_instances=1, coalesce=True,
    )
    _scheduler.add_job(
        run_monthly_exports,
        CronTrigger(day=1, hour=10, minute=0),
        id="monthly_exports_monthly_day1_1000",
        # 只在每月 1 日 10:00 生成上月文件并通知一次。不能在进程启动时补跑，
        # 否则服务重启或多实例启动会重复触发管理员工作通知。
        misfire_grace_time=86400,
    )
    _scheduler.add_job(
        backfill_monthly_exports,
        id="monthly_exports_startup_backfill",
        # 仅补齐缺失文件，不调用通知逻辑，因此重启不会重复发提醒。
        next_run_time=datetime.now(_scheduler.timezone),
        misfire_grace_time=300,
    )
    _scheduler.start()
    print("  Scheduler started")

def shutdown_scheduler():
    global _scheduler
    if _scheduler:
        scheduler = _scheduler
        _scheduler = None
        if scheduler.running:
            scheduler.shutdown(wait=False)
