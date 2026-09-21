import sqlite3
conn = sqlite3.connect('imu_data.db')
c = conn.cursor()
c.execute("SELECT * FROM help_requests ORDER BY created_at DESC LIMIT 5")
rows = c.fetchall()
print(f"Help requests count: {len(rows)}")
for row in rows:
    print(row)
conn.close()
