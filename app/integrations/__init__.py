"""Clients for external services, each isolated from business logic: they
build requests, call the service with timeouts and retries, and turn the
response into the app's own types. Business modules call them; they never
call back into routes. Where a client implements an interface a module
defines (an AI provider, a notifier), it imports that module's data types."""
