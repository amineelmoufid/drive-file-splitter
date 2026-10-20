from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/drive']

def main():
    print("Launching browser for 1-time authentication...")
    flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
    creds = flow.run_local_server(port=0)

    with open('token.json', 'w') as token_file:
        token_file.write(creds.to_json())

    print("\nSUCCESS: 'token.json' generated successfully!")
    print("From now on, authentication will be 100% automatic with NO popups and NO sharing needed.")

if __name__ == '__main__':
    main()
