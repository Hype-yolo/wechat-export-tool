-- 微信导出.app 的启动逻辑：起本地服务 + 打开向导界面。
-- 编译成 .app： osacompile -o "微信导出.app" launcher.applescript
on run
	set appPath to POSIX path of (path to me)
	set guiDir to do shell script "cd " & quoted form of appPath & "/.. && pwd"
	set rootDir to do shell script "cd " & quoted form of guiDir & "/.. && pwd"
	set venvPy to rootDir & "/.venv/bin/python"

	-- 没装环境 → 提示先跑安装脚本
	if (do shell script "test -x " & quoted form of venvPy & " && echo ok || echo no") is "no" then
		display dialog "还没装运行环境。请先双击同一文件夹里的「安装环境.command」跑一次。" buttons {"好"} default button 1 with title "微信导出" with icon caution
		return
	end if

	set theURL to "http://127.0.0.1:47653"
	-- 服务没在跑就后台起一个
	set alive to do shell script "curl -s -m 1 " & theURL & "/api/ping >/dev/null 2>&1 && echo yes || echo no"
	if alive is "no" then
		do shell script quoted form of venvPy & " " & quoted form of (guiDir & "/server.py") & " >/tmp/wechat-export-gui.log 2>&1 &"
		delay 1.5
	end if

	-- 优先用 Chrome 的 app 窗口（更像独立应用），没有就用默认浏览器
	if (do shell script "test -d '/Applications/Google Chrome.app' && echo ok || echo no") is "ok" then
		do shell script "open -na 'Google Chrome' --args --app=" & theURL & " >/dev/null 2>&1"
	else
		do shell script "open " & theURL
	end if
end run
