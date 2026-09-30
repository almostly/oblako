
.PHONY: up down pull status ollama-pull test test-unit test-s3 test-redshift test-redshift-data test-rds test-rds-data test-sagemaker test-redshift-ml test-opensearch test-stepfunctions test-ollama test-integration

up:
	docker compose up -d

down:
	docker compose down

pull:
	docker compose pull

status:
	docker compose ps

ollama-pull:
	docker exec oblako-ml-ollama-1 ollama pull llama3.2

# All tests (unit + integration, requires all services running)
test:
	python -m pytest tests/ -v

# Unit tests only (no services needed)
test-unit:
	python -m pytest tests/bedrock/test_bedrock_adapter.py -v

# Integration tests (each requires its service running)
test-s3:
	python -m pytest tests/s3/test_s3proxy.py -v

test-redshift:
	python -m pytest tests/redshift/test_redshift.py -v

test-redshift-data:
	python -m pytest tests/redshift/test_redshift_data.py -v

test-rds:
	python -m pytest tests/rds/test_rds.py -v

test-rds-data:
	python -m pytest tests/rds/test_rds_data.py -v

test-sagemaker:
	python -m pytest tests/sagemaker/ -v   # needs: pip install 'oblako[sagemaker]'

test-redshift-ml:
	python -m pytest tests/redshift/test_redshift_ml.py -v   # needs: pip install 'oblako[sagemaker]'

test-openrouter:
	python -m pytest tests/bedrock/test_bedrock_openrouter_live.py -v   # needs OPENROUTER_API_KEY (live, paid)

test-opensearch:
	python -m pytest tests/opensearch/test_opensearch.py -v

test-stepfunctions:
	python -m pytest tests/stepfunctions/test_stepfunctions.py -v

test-ollama:
	python -m pytest tests/bedrock/test_ollama.py -v

test-integration:
	python -m pytest tests/ -v --ignore=tests/bedrock/test_bedrock_adapter.py
