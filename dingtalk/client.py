import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from config.settings import settings


class DingTalkClient:
    BASE_URL = "https://oapi.dingtalk.com"

    def __init__(self):
        self.app_key = settings.dingtalk_app_key
        self.app_secret = settings.dingtalk_app_secret
        self._token = None
        self._expire_at = 0

    def _access_token(self) -> str:
        if self._token and time.time() < self._expire_at - 300:
            return self._token
        if not self.app_key or not self.app_secret:
            raise RuntimeError("DingTalk creds not configured in .env")
        r = requests.get(f"{self.BASE_URL}/gettoken",
                         params={"appkey": self.app_key, "appsecret": self.app_secret}, timeout=10)
        d = r.json()
        if d.get("errcode") != 0:
            raise RuntimeError(f"DingTalk token fail: {d}")
        self._token = d["access_token"]
        self._expire_at = time.time() + d.get("expires_in", 7200)
        return self._token

    def get(self, path, params=None):
        params = params or {}
        params["access_token"] = self._access_token()
        return requests.get(f"{self.BASE_URL}{path}", params=params, timeout=10).json()

    def post(self, path, json_data=None):
        url = f"{self.BASE_URL}{path}?access_token={self._access_token()}"
        return requests.post(url, json=json_data or {}, timeout=10).json()

    def get_user_by_auth_code(self, auth_code):
        d = self.post("/topapi/v2/user/getuserinfo", {"code": auth_code})
        if d.get("errcode") != 0:
            raise RuntimeError(f"authCode fail: {d}")
        return d["result"]

    def get_user_detail(self, userid):
        response = self.get("/topapi/v2/user/get", {"userid": userid})
        if response.get("errcode") not in (None, 0):
            raise RuntimeError(f"DingTalk user detail fail: {response}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"DingTalk user detail returned no result for {userid}")
        return result
    # ============ 部门 API ============

    def list_sub_departments(self, dept_id=1):
        """获取子部门列表。根 dept_id=1。"""
        response = self.post("/topapi/v2/department/listsub", {"dept_id": dept_id})
        if response.get("errcode") not in (None, 0):
            raise RuntimeError(
                f"DingTalk department list failed for {dept_id}: "
                f"{response.get('errmsg') or response.get('errcode')}"
            )
        result = response.get("result") or []
        if not isinstance(result, list):
            raise RuntimeError(f"DingTalk department list returned invalid result for {dept_id}")
        return result

    def get_department(self, dept_id):
        """获取单个部门详情。"""
        return self.get("/topapi/v2/department/get", {"dept_id": dept_id}).get("result", {})

    def get_root_department(self):
        """获取根部门（通常是公司名）。"""
        return self.get_department(1)

    def get_full_dept_tree(self):
        """分层并发拉取部门树，并防止异常循环关系造成无限递归。"""
        self._access_token()
        roots = self.list_sub_departments(1)
        seen = set()

        def make_node(item):
            dept_id = str(item.get("dept_id") or "").strip()
            if not dept_id:
                raise RuntimeError("DingTalk department item is missing dept_id")
            return {
                "dept_id": dept_id, "name": item.get("name", ""),
                "parent_id": str(item.get("parent_id", "")),
                "sort": item.get("order", 0) or 0, "children": [],
            }

        current = [make_node(item) for item in roots]
        result = current
        depth = 0
        while current:
            depth += 1
            if depth > 20:
                raise RuntimeError("钉钉部门层级超过20级，疑似存在循环关系")
            ids = []
            for node in current:
                if node["dept_id"] in seen:
                    raise RuntimeError(f"钉钉部门存在重复或循环 ID：{node['dept_id']}")
                seen.add(node["dept_id"])
                ids.append(node["dept_id"])
            children_by_id = {}
            with ThreadPoolExecutor(max_workers=min(8, len(ids))) as executor:
                futures = {executor.submit(self.list_sub_departments, dept_id): dept_id for dept_id in ids}
                for future in as_completed(futures):
                    children_by_id[futures[future]] = future.result()
            next_level = []
            for node in current:
                children = [make_node(item) for item in children_by_id.get(node["dept_id"], [])]
                children.sort(key=lambda item: item["sort"])
                node["children"] = children
                next_level.extend(children)
            current = next_level
        result.sort(key=lambda item: item["sort"])
        return result

    # ============ 用户 API (扩展) ============

    def list_users_by_dept(self, dept_id=1, recursive=True):
        """分页获取部门下的全部用户，避免只返回第一页（最多 100 人）。"""
        users = []
        cursor = 0
        while True:
            response = self.post("/topapi/v2/user/list", {
                "dept_id": dept_id, "cursor": cursor, "size": 100,
                "order_field": "entry_desc", "contain_access_users": recursive,
            })
            if response.get("errcode") not in (None, 0):
                raise RuntimeError(f"DingTalk member list fail: {response}")
            result = response.get("result", {}) or {}
            users.extend(result.get("list", []) or [])
            if not result.get("has_more"):
                break
            next_cursor = result.get("next_cursor")
            if next_cursor is None or next_cursor == cursor:
                raise RuntimeError("DingTalk member list returned an invalid next_cursor")
            cursor = next_cursor
        return users

    def list_all_users(self, department_ids):
        """Read every department concurrently and de-duplicate multi-department members."""
        normalized_ids = {"1"}
        normalized_ids.update(
            str(value).strip() for value in department_ids if str(value).strip()
        )
        merged = {}
        with ThreadPoolExecutor(max_workers=min(8, len(normalized_ids))) as executor:
            futures = {
                executor.submit(self.list_users_by_dept, dept_id, False): dept_id
                for dept_id in normalized_ids
            }
            for future in as_completed(futures):
                for item in future.result():
                    userid = str(item.get("userid") or "").strip()
                    if not userid:
                        continue
                    if userid not in merged:
                        merged[userid] = dict(item)
                        merged[userid]["dept_id_list"] = list(item.get("dept_id_list", []) or [])
                        continue
                    existing = merged[userid]
                    department_values = {
                        str(value) for value in existing.get("dept_id_list", []) or []
                    }
                    department_values.update(
                        str(value) for value in item.get("dept_id_list", []) or []
                    )
                    existing["dept_id_list"] = sorted(department_values)
                    for key, value in item.items():
                        if value not in (None, "", []) and existing.get(key) in (None, "", []):
                            existing[key] = value
        return [merged[key] for key in sorted(merged)]

    def send_work_notification(self, userid, title, markdown, action_title, action_url):
        """向一个或多个员工发送企业内部应用工作通知（userid 可用逗号分隔）。"""
        agent_id = str(settings.dingtalk_agent_id or "").strip()
        if not agent_id:
            raise RuntimeError("DINGTALK_AGENT_ID 未配置，无法发送工作通知")
        userid = str(userid or "").strip()
        if not userid:
            raise RuntimeError("接收人的钉钉 userid 为空，无法发送工作通知")
        payload = {
            "agent_id": int(agent_id) if agent_id.isdigit() else agent_id,
            "userid_list": userid,
            "msg": {
                "msgtype": "action_card",
                "action_card": {
                    "title": str(title),
                    "markdown": str(markdown),
                    "single_title": str(action_title),
                    "single_url": str(action_url),
                },
            },
        }
        response = self.post("/topapi/message/corpconversation/asyncsend_v2", payload)
        if response.get("errcode") not in (None, 0):
            raise RuntimeError(f"DingTalk work notification failed: {response}")
        return response

    def get_hrm_field_groups(self):
        """只读获取智能人事花名册字段元数据。"""
        response = self.post(
            "/topapi/smartwork/hrm/employee/field/grouplist",
            {"agentid": settings.dingtalk_agent_id},
        )
        if response.get("errcode") not in (None, 0):
            raise RuntimeError(f"DingTalk HRM field metadata failed: {response}")
        return response.get("result", []) or []

    def find_hrm_field_code(self, field_name):
        for group in self.get_hrm_field_groups():
            for field in group.get("field_list", []) or []:
                if str(field.get("field_name") or "").strip() == field_name:
                    return str(field.get("field_code") or "").strip()
        return ""

    def list_hrm_employee_fields(self, userids):
        """只读获取最多 20 名员工花名册字段；调用方只保留审批所需字段。"""
        normalized = [str(value).strip() for value in userids if str(value).strip()]
        if not normalized:
            return []
        if len(normalized) > 20:
            raise ValueError("智能人事单次最多读取 20 名员工")
        # 自定义下拉字段在 field_filter_list 模式下可能不会返回，因此不传过滤器，
        # 收到响应后仅提取“职务”和“直属主管Id”，不落库其他人事字段。
        response = self.post("/topapi/smartwork/hrm/employee/list", {
            "agentid": settings.dingtalk_agent_id,
            "userid_list": ",".join(normalized),
        })
        if response.get("errcode") not in (None, 0):
            raise RuntimeError(f"DingTalk HRM employee list failed: {response}")
        return response.get("result", []) or []
