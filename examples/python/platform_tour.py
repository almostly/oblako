"""Full platform lifecycle via docker-py.

Start all services, check health, print status, stop everything.
No docker-compose CLI needed.

Prerequisites:
    Docker running
"""

from oblako.services import Oblako

oblako = Oblako()

print("Starting oblako...")
oblako.up()

print("\nWaiting for services to be ready...")
readiness = oblako.wait_ready(timeout=60)
for name, ready in readiness.items():
    status = "ready" if ready else "not ready"
    print(f"{name}: {status}")

print("\nStatus:")
for name, state in oblako.status().items():
    print(f"{name}: {state}")

# Show Ollama models if available
if readiness.get("ollama"):
    models = oblako.ollama.list_models()
    if models:
        print(f"\nOllama models: {', '.join(models)}")
    else:
        print("\nNo models pulled yet. Run: oblako pull")

input("\nPress Enter to stop all services...")
oblako.down()
print("Done.")
