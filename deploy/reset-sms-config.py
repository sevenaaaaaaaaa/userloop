import json

p = "/www/wwwroot/userloop/data/config.json"
cfg = json.load(open(p, encoding="utf-8"))
cfg["touch"]["sms"] = {
    "enabled": False,                      # 填好凭据后改 true 即生效
    "provider": "aliyun",                  # aliyun | tencent | webhook
    "sign_name": "",                       # 报备通过的短信签名
    "template_code": "",                   # 报备通过的模板号
    "access_key_id": "",
    "access_key_secret": "",
    "template_param": {"content": "{content}"},
    "append_unsubscribe": True,            # 自动附加「回T退订」合规指令
}
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print("RESET_OK")
