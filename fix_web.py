with open('/Users/wuqiong/aibubu_outbound_engine/web/server.py', 'rb') as f:
    data = f.read()

# Replace any invalid literal newlines
fixed = data.replace(b'print("
\xe6\x9c\x8d\xe5\x8a\xa1\xe5\xb7\xb2\xe5\x85\xb3\xe9\x97\xad")', b'print("\n\xe6\x9c\x8d\xe5\x8a\xa1\xe5\xb7\xb2\xe5\x85\xb3\xe9\x97\xad")')

with open('/Users/wuqiong/aibubu_outbound_engine/web/server.py', 'wb') as f:
    f.write(fixed)
print("Binary fixed!")
