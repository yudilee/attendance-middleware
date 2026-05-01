import requests

session = requests.Session()
response = session.post("http://localhost:8999/login", data={"username": "admin", "password": "admin"}, allow_redirects=False)
print("Login Status:", response.status_code)
if "dashboard_session" in session.cookies:
    res2 = session.get("http://localhost:8999/")
    print("Dashboard Status:", res2.status_code)
    if res2.status_code == 500:
        print(res2.text)
else:
    print("Failed to get cookie")
