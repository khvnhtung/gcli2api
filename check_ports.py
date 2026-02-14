import socket

def check_listening():
    for family in [socket.AF_INET, socket.AF_INET6]:
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            result = sock.connect_ex(('localhost', 7861))
            if result == 0:
                print(f"Listening on {family.name}")
            sock.close()
        except Exception as e:
            print(f"Error checking {family.name}: {e}")

if __name__ == "__main__":
    check_listening()
