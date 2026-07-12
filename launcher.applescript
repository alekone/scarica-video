-- Scarica Video — launcher: avvia il server locale e apre la finestra dedicata
set thePort to "8642"
set theURL to "http://127.0.0.1:" & thePort & "/"
set appPy to quoted form of (POSIX path of (path to resource "app.py"))

-- Il server è già attivo?
set isUp to "000"
try
	set isUp to (do shell script "curl -s -o /dev/null -w '%{http_code}' " & theURL & "ping")
end try

if isUp is not "200" then
	-- avvia il server in background (staccato dall'app)
	do shell script "export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH; nohup /usr/bin/python3 " & appPy & " >/dev/null 2>&1 &"
	-- attendi che risponda (max ~6s)
	repeat 30 times
		delay 0.2
		try
			set isUp to (do shell script "curl -s -o /dev/null -w '%{http_code}' " & theURL & "ping")
		end try
		if isUp is "200" then exit repeat
	end repeat
end if

-- apri la finestra in modalità app (senza barra indirizzi) col primo browser Chromium disponibile
set chromiumBins to {"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "/Applications/Chromium.app/Contents/MacOS/Chromium"}
set opened to false
repeat with binPath in chromiumBins
	set p to binPath as text
	if (do shell script "if [ -x " & quoted form of p & " ]; then echo yes; else echo no; fi") is "yes" then
		do shell script quoted form of p & " --app=" & theURL & " --window-size=760,900 >/dev/null 2>&1 &"
		set opened to true
		exit repeat
	end if
end repeat

if not opened then
	-- nessun browser Chromium: apri nel browser predefinito
	do shell script "open " & quoted form of theURL
end if
