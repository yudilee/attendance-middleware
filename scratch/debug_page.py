import requests

session = requests.Session()
login_res = session.post("http://localhost:8999/login", data={
    "username": "admin",
    "password": "admin"
})

dashboard_res = session.get("http://localhost:8999/ui")
with open("scratch/rendered.html", "w") as f:
    f.write(dashboard_res.text)

print("Rendered HTML size:", len(dashboard_res.text))
