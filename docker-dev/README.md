# Waldur Mastermind Docker Compose Development Environment

## Instructions

You must have [Docker](https://www.docker.com/) with [Docker Compose](https://docs.docker.com/compose/) installed.
 
```bash
git clone https://github.com/opennode/waldur-mastermind.git
```

```bash
cd docker-dev
```

```bash
docker-compose up -d
```

Wait for the Mastermind Booting.. at the first time may take some minutes, in order to run migrations and user creation.

Then, load http://localhost:8000

For the login there is one user:

Username: staff
Password: querty

Enjoy.
