# Douyin Chat GIF Capture

用 Playwright 接管已经登录的 Chrome，从抖音网页版聊天记录里抓取指定日期的 GIF 表情包。

这个脚本来自一次实际跑通的抖音聊天抓取流程，核心经验是：

- 重启 Chrome，让抖音 React 聊天组件重新干净挂载。
- 用 CDP `Network.setCacheDisabled` 真正禁用缓存，确保滚动到 GIF 时会触发网络响应。
- 用真实 `page.mouse.wheel()` 滚动，不直接修改 `scrollTop`。
- 日期分隔符只读取 `.MessageBoxTimetimeLayout`。
- 抖音最近 7 天内可能显示为 `昨天`、`前天`、`周五` 这类文本，而不是 `05/23`。
- wheel 后用 100ms 轮询日期节点，避免虚拟列表节点短暂出现后被回收。

## 安装

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## 启动 Chrome

先关闭已有 Chrome，然后用远程调试端口启动。示例：

```powershell
taskkill /F /IM chrome.exe
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=10222 `
  --user-data-dir="D:\douyin_chrome_profile" `
  --profile-directory=user
```

打开后先确认抖音已登录。

## 运行

```bash
python capture_douyin_chat_gifs.py ^
  --friend-name "好友昵称" ^
  --target-date 2026-05-23 ^
  --today 2026-05-25 ^
  --cdp-url http://127.0.0.1:10222
```

参数说明：

- `--friend-name`：目标好友昵称。如果当前已经进入目标聊天页，可以不填。
- `--target-date`：要抓取的聊天日期，格式 `YYYY-MM-DD`。
- `--today`：脚本运行当天，格式 `YYYY-MM-DD`。建议显式传入，避免跨时区或隔天运行误判。
- `--download-dir`：图片保存目录，默认 `downloads/`。
- `--log-dir`：日志目录，默认 `logs/`。
- `--no-download`：只抓 URL，不下载图片。

示例只抓 URL：

```bash
python capture_douyin_chat_gifs.py --friend-name "好友昵称" --target-date 2026-05-23 --today 2026-05-25 --no-download
```

## 输出

- `downloads/`：下载的 GIF 文件。
- `logs/wheel_gif_urls.txt`：抓到的 GIF URL。
- `logs/failed.txt`：下载失败记录。
- `logs/summary.json`：抓取汇总。

## 注意

这个工具依赖抖音网页端当前 DOM 和网络行为，抖音改版后可能需要重新调整选择器或日期规则。公开分享时不要提交自己的 `downloads/`、`logs/`、浏览器用户数据目录，也不要把好友昵称、Cookie 或个人路径写进提交。
