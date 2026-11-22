from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_host: str = "0.0.0.0"
    app_port: int = 26666
    debug: bool = False
    database_url: str = "sqlite:///./express_v2.db"

    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    dingtalk_agent_id: str = ""
    dingtalk_corp_id: str = ""
    dingtalk_oa_process_code: str = ""
    dingtalk_event_token: str = ""
    dingtalk_event_aes_key: str = ""
    dingtalk_login_enabled: bool = False
    public_base_url: str = ""
    session_secret: str = ""
    session_cookie_secure: bool = True
    session_max_age: int = 43200
    scheduler_enabled: bool = True
    # Local real-data previews preserve the visible production configuration,
    # while this switch prevents every carrier client from placing live orders.
    preview_safe_mode: bool = False
    # Explicit local launcher identity. It is ignored whenever DingTalk login
    # is enabled, and keeps local order ownership scoped to one real user.
    local_user_id: int = 0

    # 有成 CRM 只读接口：统一同步项目及部门基础资料。
    # Public repository intentionally has no organization-specific CRM endpoint.
    crm_base_url: str = ""
    crm_app_id: str = ""
    crm_app_secret: str = ""
    crm_request_timeout: int = 30
    crm_request_retries: int = 2
    crm_page_size: int = 200

    # 旧版系统寄件记录月结归档；新的承运商账单分摊文件使用 billing_allocation_dir。
    monthly_export_dir: str = ".runtime/monthly-exports"
    # 承运商实际账单及按运单匹配生成的分摊文件。
    billing_allocation_dir: str = ".runtime/billing-allocations"

    # 顺丰丰桥：生产和沙箱使用不同网关，必须明确配置。
    sf_api_url: str = "https://bspgw.sf-express.com/std/service"
    sf_customer_code: str = ""       # partnerID
    sf_check_word: str = ""          # checkWord，禁止提交到 Git
    sf_monthly_card: str = ""        # 可留空，由顺丰应用/客户关系绑定决定结算
    sf_mock_mode: bool = True
    sf_live_order_enabled: bool = False
    sf_api_timeout: int = 20
    # 寄件页实时试算使用更短的独立超时，避免慢接口拖住整个页面。
    # 正式下单、取消和物流查询仍使用各承运商自己的 API 超时。
    courier_quote_timeout: float = 4.0
    courier_quote_total_timeout: float = 8.0
    courier_quote_cache_seconds: int = 300
    # 顺丰同城回调可选令牌。设置后，后台 URL 需追加 ?token=同一值。
    sf_city_callback_token: str = ""
    # 顺丰同城开放平台。测试开发者与正式开发者分开保存，
    # 联调工具只允许使用下面的测试凭证。
    sf_city_api_url: str = "https://openic.sf-express.com"
    sf_city_feature_enabled: bool = False
    sf_city_dev_id: str = ""
    sf_city_dev_key: str = ""
    sf_city_shop_id: str = ""
    sf_city_live_order_enabled: bool = False
    # 正式同城订单等待骑士接单的最长时间。到时仍无人接单时，后台会
    # 先实时核对一次状态，再调用顺丰同城取消接口。
    sf_city_rider_wait_minutes: int = 30
    sf_city_test_dev_id: str = ""
    sf_city_test_dev_key: str = ""
    sf_city_test_company_id: str = ""
    sf_city_test_shop_id: str = ""
    sf_city_test_account_phone: str = ""
    # 上传内容在读入内存前的统一上限，默认 5 MiB。
    max_upload_bytes: int = 5 * 1024 * 1024

    # 德邦新开放平台：各业务分别有固定的正式地址，不拼接旧版路径。
    db_create_order_url: str = "https://gwapi.deppon.com/dop-interface-async/standard-order/createOrderNotify.action"
    db_query_order_url: str = "https://dpapi.deppon.com/dop-interface-sync/standard-query/queryOrder.action"
    db_cancel_order_url: str = "https://gwapi.deppon.com/dop-interface-async/standard-order/cancelOrder.action"
    db_trace_query_url: str = "https://dpapi.deppon.com/dop-interface-sync/standard-query/newTraceQuery.action"
    db_price_query_url: str = "https://dpapi.deppon.com/dop-interface-sync/standard-query/queryPriceTime.action"
    db_waybill_fee_url: str = "https://dpapi.deppon.com/dop-interface-sync/standard-query/sscWayBillQueryMsg.action"
    db_company_code: str = ""
    db_customer_code: str = ""        # 月结账号
    db_appkey: str = ""               # 签名密钥，禁止提交到 Git
    db_sign: str = ""                 # 平台分配的 sign 前缀
    db_mock_mode: bool = True
    db_live_order_enabled: bool = False
    db_api_timeout: int = 20

    # 京东物流“京东快递快运”开放接口。
    jdl_api_url: str = "https://api.jdl.com"
    jdl_app_key: str = ""
    jdl_app_secret: str = ""
    jdl_access_token: str = ""
    jdl_customer_code: str = ""       # 月结编码
    jdl_business_unit_code: str = ""  # 快运事业部编码（大件产品必填）
    jdl_api_timeout: int = 20
    jdl_live_order_enabled: bool = False

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
