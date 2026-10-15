# A single worker owns the in-memory queue and session-secret fallback.
def worker_exit(server, worker):
    import cloud
    cloud.shutdown()
