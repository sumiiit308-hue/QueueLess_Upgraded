QUEUELESS - UPGRADED UNIVERSITY QUEUE MANAGEMENT
================================================

WHAT'S NEW
----------
1. Multiple service counters per department.
   - New tokens are automatically assigned to the least-loaded active counter.
   - Staff can call the next token for a specific counter.
   - Add, pause and enable counters from Admin > Manage campus.

2. Analytics dashboard.
   - Daily token volume for the latest 7 days.
   - 30-day token and completion totals.
   - Department-level volume and average service duration.

3. Student notifications.
   - In-app notification centre in the student portal.
   - Alerts when the student is among the next two in line or when the token is serving.
   - Optional browser notifications (allow permission when clicking Alerts).
   - Keep the student dashboard open for live queue updates.

4. Dynamic university / department management.
   - Change the university name shown in the student portal.
   - Add departments and services without editing Python source.
   - Add, pause and enable counters.

5. Refreshed responsive frontend.
   - Redesigned student dashboard, service cards, token card and notification centre.
   - Updated sign-in / registration styling.
   - Admin analytics and campus settings pages.

INSTALL / RUN (WINDOWS)
-----------------------
1. Extract the ZIP file.
2. Open the extracted QueueLess_Upgrade folder.
3. Open Command Prompt in that folder.
4. Run:  py -m venv venv
5. Run:  venv\Scripts\activate
6. Run:  pip install -r requirements.txt
7. Run:  py app.py
8. Open: http://127.0.0.1:5000

ADMIN LOGIN
-----------
Open the Admin portal link on the sign-in page.
Default local demo password: admin123
Change this before using the application beyond a local demonstration:
  set QUEUELESS_ADMIN_PASSWORD=your-strong-password
You can also configure QUEUELESS_SECRET_KEY with a long random value.

DATABASE / EXISTING DATA
------------------------
The existing queueless.db file is included. The application performs additive
SQLite migrations on startup: existing users and tokens are retained, and the
new departments, counters, notification and settings tables are added.
Keep a backup of queueless.db before making changes or running a demo.

NOTES
-----
- This is a local academic/demo Flask application, not a production deployment.
- Analytics are calculated from records in the local SQLite database.
- Browser notifications require the student portal to be open and browser
  notification permission to be granted. In-app alerts are also available.
- Wait-time prediction remains an estimate based on recent completed services.
