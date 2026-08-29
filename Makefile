# RegEngine AI -- root Makefile
#
# Targets operate across every service under services/*, plus
# frontend/ and deploy/. Run `make help` for a summary.

SERVICES := ingestion agents compiler execution audit
IMAGE_REGISTRY ?= ghcr.io/your-org
IMAGE_TAG ?= latest
NAMESPACE ?= regengine
HELM_RELEASE ?= regengine

.PHONY: help build test dev deploy clean lint

help:
	@echo "Targets:"
	@echo "  make build   - Build a Docker image for every service + the frontend"
	@echo "  make test    - Run each service's test suite (pytest) and the frontend's (vitest)"
	@echo "  make dev     - Bring up the full local stack via docker compose"
	@echo "  make deploy  - Helm upgrade/install the regengine chart into \$$NAMESPACE"
	@echo "  make lint    - Run ruff/black --check across every Python service"
	@echo "  make clean   - Tear down the local docker compose stack"

build:
	@for svc in $(SERVICES); do \
		echo "==> building services/$$svc"; \
		docker build -t $(IMAGE_REGISTRY)/regengine-$$svc:$(IMAGE_TAG) services/$$svc; \
	done
	@echo "==> building frontend"
	docker build -t $(IMAGE_REGISTRY)/regengine-frontend:$(IMAGE_TAG) frontend

test:
	@for svc in $(SERVICES); do \
		echo "==> testing services/$$svc"; \
		( cd services/$$svc && python -m pytest -q ) || exit 1; \
	done
	@if [ -d frontend/node_modules ]; then \
		echo "==> testing frontend"; \
		( cd frontend && npm test ); \
	else \
		echo "==> skipping frontend tests (run 'npm install' in frontend/ first)"; \
	fi

dev:
	docker compose -f docker-compose.yml up --build

deploy:
	helm upgrade --install $(HELM_RELEASE) deploy/helm/regengine \
		--namespace $(NAMESPACE) --create-namespace \
		--set image.registry=$(IMAGE_REGISTRY) \
		--set image.tag=$(IMAGE_TAG)

lint:
	@for svc in $(SERVICES); do \
		echo "==> linting services/$$svc"; \
		ruff check services/$$svc; \
	done

clean:
	docker compose -f docker-compose.yml down -v
