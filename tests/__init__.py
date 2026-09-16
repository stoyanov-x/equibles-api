# Marks tests as a package so one test module can import helpers from another
# (test_openapi reuses the fake query source and the request helper from
# test_router rather than keeping a second copy in sync).
