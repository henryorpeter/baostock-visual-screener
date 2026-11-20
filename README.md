# Baostock 可视化选股

## 安装运行

在本目录打开 PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run baostock_screener.py
```

首次点击侧栏“更新全市场行情缓存”。行情写入脚本旁的 `stock_cache.sqlite3`；这次全量更新需要等待并显示进度。随后点击“开始本地选股”，扫描只从 SQLite 读取。之后再次更新会跳过已缓存区间，只补近期行情；缓存文件也可复制到其他电脑继续使用。

行情优先走 Baostock 单连接；连接失败的股票通过腾讯 HTTPS 最多 6 路并发补齐。股票列表依次尝试 Baostock、同花顺 HTTPS、腾讯 HTTPS 实时报价核验。页面会显示更新、跳过、失败和有效缓存数量；更新失败项可再次重试。

如果所在网络无法访问 Baostock 的 `public-api.baostock.com:10030`，工具会切换 HTTPS 数据源。备用源仍需要能访问同花顺或腾讯财经域名。缓存齐备后，日常选股过程不访问行情网络。腾讯备用日线没有换手率，换手率数据会留空，相关换手率条件自动跳过。

默认扫描上限为 6,000 只；可调小以缩短本地计算时间。支持多选策略、查看走势图和导出 CSV。Sequoia-X 参考策略包括海龟突破、高旗形整理、涨停洗盘、上升趋势跌停反包、RPS 强势突破；RPS 按本次扫描样本排名。图表提供日K蜡烛、MA5/10/20/30、成交量和区间缩放。微信推送功能暂时从界面隐藏。
