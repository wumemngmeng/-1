import os

script_path = "/Users/wuqiong/aibubu_outbound_engine/cli.py"
with open(script_path, "r", encoding="utf-8") as f:
    text = f.read()

# Replace any literal newlines that broke print statements
fixed_text = text.replace('print(f"
', 'print(f"\n')
fixed_text = fixed_text.replace('print("
', 'print("\n')
fixed_text = fixed_text.replace('
")', '\n")')
fixed_text = fixed_text.replace('print("
【LinkedIn', 'print("\n【LinkedIn')

with open(script_path, "w", encoding="utf-8") as f:
    f.write(fixed_text)
print("Sanitized newlines in cli.py")
