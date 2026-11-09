# Recorded from the repository root after the quickstart install:
#   uv venv && uv pip install -e . && source .venv/bin/activate
# Renders assets/demo.gif. The 24-invoice fixture ships in examples/invoices/.
Output assets/demo.gif

Set Shell "zsh"
Set FontFamily "Menlo"
Set FontSize 16
Set LineHeight 1.4
Set Width 1180
Set Height 660
Set Padding 18
Set Theme "tokyonight-storm"
Set TypingSpeed 40ms
Set Framerate 20

Sleep 500ms
Hide
Type "source .venv/bin/activate; PROMPT='%F{214}deltagate%f %F{245}repo%f %F{214}$%f '; clear"
Enter
Sleep 700ms
Show

Type "echo '-- 24 invoices: accepted baseline vs the re-run after a quant swap'"
Enter
Sleep 1400ms

Type "deltagate check \"
Enter
Sleep 260ms
Type "  --baseline examples/invoices/accepted \"
Enter
Sleep 260ms
Type "  --candidate examples/invoices/rerun \"
Enter
Sleep 260ms
Type "  --rules examples/invoices/rules.yaml"
Enter
Sleep 8500ms

Type "cat out/review.csv"
Enter
Sleep 5200ms

Type "ls -1 out"
Enter
Sleep 4200ms

Type "python -m json.tool out/manifest.json | head -21"
Enter
Sleep 7000ms

Type "echo 'exit 3: only these four records need a human'"
Enter
Sleep 4200ms
