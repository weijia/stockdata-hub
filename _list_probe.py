import akshare as ak, logging
logging.basicConfig(level=logging.WARNING)
for name in ["stock_info_a_code_name", "stock_zh_a_spot_tx", "stock_zh_a_spot_em"]:
    try:
        fn = getattr(ak, name)
        df = fn()
        print(name, "OK rows=", len(df), list(df.columns)[:6])
    except Exception as e:
        print(name, "FAIL", repr(e)[:150])
