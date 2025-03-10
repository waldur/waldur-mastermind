bind = ":8080"
workers = 4
forwarded_allow_ips = "*"
proxy_allow_ips = "*"
accesslog = "-"
access_log_format = (
    '%({X-Forwarded-For}i)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'
)
errorlog = "-"
capture_output = True
