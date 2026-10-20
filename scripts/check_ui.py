import httpx

r = httpx.get("http://localhost:8000/")
print(f"Status: {r.status_code}")
print(f"Content-Type: {r.headers.get('content-type', '')}")
print(r.text[:300])
