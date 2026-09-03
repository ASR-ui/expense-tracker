import bcrypt
salt = bcrypt.gensalt()
# Replace "your_old_password" with your actual password inside the quotes
print(bcrypt.hashpw(b"your_old_password", salt).decode('utf-8'))