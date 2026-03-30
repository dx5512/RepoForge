from lark_oapi.api.im.v1 import CreateMessageRequest
import json

print("Testing CreateMessageRequest builder...")

# Test RequestBody builder
rb = CreateMessageRequest.RequestBody.builder()
print("RequestBody methods:", [m for m in dir(rb) if not m.startswith('_')])

# Build a request body
body = rb.receive_id('test_chat_id').msg_type('text').content(json.dumps({"text": "hello"})).build()
print("Body built:", body)

# Build complete request
request = (
    CreateMessageRequest.builder()
    .receive_id_type("chat_id")
    .request_body(body)
    .build()
)
print("Request built successfully!")
print("Request:", request)
