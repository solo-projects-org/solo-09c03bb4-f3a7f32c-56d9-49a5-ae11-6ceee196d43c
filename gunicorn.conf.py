bind = "0.0.0.0:8000"
workers = 1          # SQLite 写入串行化，单 worker 最稳
threads = 4
timeout = 60
accesslog = "-"
errorlog = "-"
