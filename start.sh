#!/bin/sh
python bot.py &
BOT_PID=$!
python server.py
kill $BOT_PID 2>/dev/null
