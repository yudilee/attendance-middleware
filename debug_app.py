import sys
import os
import asyncio

# Add project to path
sys.path.append(os.path.join(os.getcwd(), "app"))
# Add backend to path
sys.path.append(os.getcwd())

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

try:
    print("Testing GET / ...")
    response = client.get("/")
    print(f"Status Code: {response.status_code}")
    if response.status_code == 500:
        print("Error detected! Response content:")
        print(response.text)
    else:
        print("Success!")
except Exception as e:
    print(f"Exception during request: {e}")
    import traceback
    traceback.print_exc()
