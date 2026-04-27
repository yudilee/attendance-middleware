import requests
import re
import json
import logging
from sqlalchemy.orm import Session
from app.database.models import Employee, ADMSCredential

logger = logging.getLogger(__name__)

def sync_employees_from_adms(db: Session):
    """
    Logs into the ADMS server using stored credentials,
    scrapes the employee Javascript array, and syncs to our DB.
    """
    creds = db.query(ADMSCredential).filter(ADMSCredential.is_active == True).first()
    if not creds:
        logger.warning("No active ADMS credentials found. Skipping employee sync.")
        return False, "No ADMS credentials configured."

    adms_url = creds.url.rstrip('/')
    
    session = requests.Session()
    
    try:
        # 1. Get CSRF Token
        r = session.get(f"{adms_url}/iclock/accounts/", timeout=10)
        r.raise_for_status()
        
        csrf = session.cookies.get('csrftoken')
        if not csrf:
            logger.error("Could not obtain CSRF token from ADMS.")
            return False, "Could not obtain CSRF token."

        # 2. Login
        login_data = {
            'username': creds.username,
            'password': creds.password,
            'csrfmiddlewaretoken': csrf
        }
        r = session.post(
            f"{adms_url}/iclock/accounts/", 
            data=login_data, 
            headers={"Referer": f"{adms_url}/iclock/accounts/"},
            timeout=10
        )
        r.raise_for_status()

        # 3. Fetch large page (limit=3000 to get all)
        r = session.get(f"{adms_url}/iclock/data/employee/?p=1&l=3000", timeout=30)
        r.raise_for_status()
        html = r.text

        # 4. Extract data array using regex
        match = re.search(r'data=\[(.*?)\];', html, re.DOTALL)
        if not match:
            logger.error("Could not find data array in ADMS HTML.")
            return False, "Could not find employee data in ADMS response."

        data_str = "[" + match.group(1) + "]"
        # Fix JS syntax to valid JSON
        data_str = data_str.replace('deviceText', '""')
        data_str = re.sub(r',\s*]', ']', data_str)

        try:
            users_raw = json.loads(data_str)
        except Exception as e:
            logger.error(f"Failed to parse ADMS JSON data: {e}")
            return False, "Failed to parse employee data."

        synced_count = 0
        for row in users_raw:
            if len(row) < 5:
                continue
                
            adms_id = str(row[0])
            pin = str(row[1])
            name = str(row[2]).strip()
            if not name:
                name = f"Employee {pin}"
            dept = str(row[4])

            # Update or Create
            emp = db.query(Employee).filter(Employee.employee_id == pin).first()
            if emp:
                emp.adms_id = adms_id
                emp.full_name = name
                emp.department = dept
            else:
                emp = Employee(
                    adms_id=adms_id,
                    employee_id=pin,
                    full_name=name,
                    department=dept
                )
                db.add(emp)
            
            synced_count += 1

        db.commit()
        logger.info(f"Successfully synced {synced_count} employees from ADMS.")
        return True, f"Successfully synced {synced_count} employees."

    except requests.exceptions.RequestException as e:
        logger.error(f"Network error communicating with ADMS: {e}")
        return False, f"Network error: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error during ADMS sync: {e}")
        return False, f"Unexpected error: {str(e)}"
