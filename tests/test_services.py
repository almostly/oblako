"""Unit tests for the services layer (mocked Docker client)."""

from unittest.mock import MagicMock

from oblako.services.base import Service, PortMapping, ServiceStatus


def test_service_container_name():
    svc = Service(name="test", image="alpine:latest")
    assert svc.container_name == "oblako-ml-test"


def test_service_port_bindings():
    svc = Service(
        name="test",
        image="alpine:latest",
        ports=[PortMapping(container_port=8080, host_port=9090)],
    )
    assert svc._port_bindings() == {"8080/tcp": 9090}


def test_service_status_stopped():
    svc = Service(name="test", image="alpine:latest")
    mock_client = MagicMock()
    from docker.errors import NotFound
    mock_client.containers.get.side_effect = NotFound("not found")
    svc._client = mock_client
    assert svc.status() == ServiceStatus.STOPPED


def test_service_status_running():
    svc = Service(name="test", image="alpine:latest")
    mock_client = MagicMock()
    mock_container = MagicMock()
    mock_container.status = "running"
    mock_client.containers.get.return_value = mock_container
    svc._client = mock_client
    assert svc.status() == ServiceStatus.RUNNING


def test_bedrock_service_defaults():
    from oblako.services.bedrock import BedrockService, OllamaService
    svc = BedrockService()
    assert svc.name == "bedrock"
    assert svc.image == "ollama/ollama:latest"  # Ollama is the engine
    assert svc.url == "http://localhost:11434"
    assert OllamaService is BedrockService  # backwards-compatible alias


def test_bedrock_service_runtime_client():
    from oblako.services.bedrock import BedrockService
    svc = BedrockService(runtime_port=8055)
    br = svc.get_client(autostart=False)
    assert br.meta.endpoint_url == "http://localhost:8055"
    assert br.meta.service_model.service_name == "bedrock-runtime"
    # control-plane client shares the endpoint
    ctl = svc.get_control_client(autostart=False)
    assert ctl.meta.endpoint_url == "http://localhost:8055"
    assert ctl.meta.service_model.service_name == "bedrock"


def test_redshift_service_connect_params():
    from oblako.services.redshift import RedshiftService
    svc = RedshiftService(host_port=5555, user="myuser", password="mypass", database="mydb")
    assert svc.user == "myuser"
    assert svc.host_port == 5555
    assert svc.name == "redshift"


def test_redshift_service_client_endpoints():
    from oblako.services.redshift import RedshiftService
    svc = RedshiftService(control_port=5599, data_port=8099)
    rs = svc.get_client()
    assert rs.meta.endpoint_url == "http://localhost:5599"
    assert rs.meta.service_model.service_name == "redshift"
    # data client should not require autostart to construct
    rd = svc.get_data_client(autostart=False)
    assert rd.meta.endpoint_url == "http://localhost:8099"
    assert rd.meta.service_model.service_name == "redshift-data"


def test_rds_service_defaults():
    from oblako.services.rds import RdsService
    svc = RdsService()
    assert svc.name == "rds"
    assert svc.image == "postgres:16"
    assert svc.host_port == 5432


def test_rds_service_control_client():
    from oblako.services.rds import RdsService
    svc = RdsService(control_port=5511)
    rds = svc.get_client()
    assert rds.meta.endpoint_url == "http://localhost:5511"
    assert rds.meta.service_model.service_name == "rds"


def test_rds_service_mysql_engine():
    from oblako.services.rds import RdsService
    svc = RdsService(engine="mysql")
    assert svc.engine == "mysql"
    assert svc.image == "mysql:8.0"
    assert svc.host_port == 3306
    assert svc.name == "rds"
    assert svc.environment["MYSQL_USER"] == "oblako"


def test_rds_invalid_engine():
    import pytest
    from oblako.services.rds import RdsService
    with pytest.raises(ValueError):
        RdsService(engine="oracle")


def test_rds_data_executor_engines():
    import pytest
    from oblako.rds_data.executor import RdsDataExecutor
    assert RdsDataExecutor().engine == "postgres"
    assert RdsDataExecutor(engine="mysql", port=3306).engine == "mysql"
    with pytest.raises(ValueError):
        RdsDataExecutor(engine="oracle")


def test_redshift_ml_parse_create_model():
    import pytest
    from oblako.redshift_ml import is_create_model, parse_create_model

    sql = ("CREATE MODEL m FROM (SELECT a, b, y FROM t) TARGET y FUNCTION predict_y "
           "AUTO OFF MODEL_TYPE xgboost OBJECTIVE 'binary:logistic' "
           "HYPERPARAMETERS DEFAULT EXCEPT (NUM_ROUND '50', MAX_DEPTH '4')")
    assert is_create_model(sql)
    spec = parse_create_model(sql)
    assert spec["model_type"] == "XGBOOST"
    assert spec["problem_type"] == "binary_classification"  # derived from OBJECTIVE
    assert spec["target"] == "y" and spec["function"] == "predict_y"
    assert spec["select"] == "SELECT a, b, y FROM t"
    assert spec["num_round"] == 50 and spec["max_depth"] == 4

    mlp = parse_create_model("CREATE MODEL m FROM (SELECT a, y FROM t) TARGET y "
                             "FUNCTION f MODEL_TYPE MLP PROBLEM_TYPE regression")
    assert mlp["model_type"] == "MLP" and mlp["problem_type"] == "regression"

    with pytest.raises(ValueError):
        parse_create_model("CREATE MODEL m FROM (SELECT a FROM t) TARGET a FUNCTION f MODEL_TYPE BOGUS")


def test_moto_service_defaults():
    from oblako.services.moto import MotoService
    svc = MotoService(host_port=5577)
    assert svc.name == "moto"
    assert svc.image == "motoserver/moto:latest"
    assert svc.endpoint_url == "http://localhost:5577"
    assert svc._port_bindings() == {"5000/tcp": 5577}


def test_redshift_data_field_encoding():
    import datetime
    import decimal
    from oblako.redshift_data.executor import RedshiftDataExecutor, _to_pg_array

    enc = RedshiftDataExecutor._encode_field
    assert enc(None) == {"isNull": True}
    assert enc(True) == {"booleanValue": True}
    assert enc(42) == {"longValue": 42}
    assert enc(3.5) == {"doubleValue": 3.5}
    assert enc(decimal.Decimal("1.25")) == {"stringValue": "1.25"}
    assert enc("hi") == {"stringValue": "hi"}
    assert enc(b"\x00\x01")["blobValue"]  # base64 string
    assert enc(datetime.date(2020, 1, 2)) == {"stringValue": "2020-01-02"}
    assert enc([1, 2, 3]) == {"stringValue": "{1,2,3}"}
    # bool must be checked before int (bool is a subclass of int)
    assert enc(False) == {"booleanValue": False}
    assert _to_pg_array(["a", None, "b c"]) == '{a,NULL,"b c"}'


def test_s3proxy_service_endpoint():
    from oblako.services.s3proxy import S3ProxyService
    svc = S3ProxyService(host_port=8888)
    assert svc.endpoint_url == "http://localhost:8888"


def test_stepfunctions_service_endpoint():
    from oblako.services.stepfunctions import StepFunctionsService
    svc = StepFunctionsService(host_port=9999)
    assert svc.endpoint_url == "http://localhost:9999"


def test_sagemaker_image_exists():
    from oblako.services.sagemaker import SageMakerService
    svc = SageMakerService()
    mock_client = MagicMock()
    svc._client = mock_client
    mock_client.images.get.return_value = MagicMock()
    assert svc.image_exists("my-training:latest") is True


def test_oblako_status():
    from oblako.services import Oblako
    oblako = Oblako()
    mock_client = MagicMock()
    from docker.errors import NotFound
    mock_client.containers.get.side_effect = NotFound("not found")
    mock_client.containers.list.return_value = []
    for svc in oblako._docker_services:
        svc._client = mock_client
    oblako.sagemaker._client = mock_client

    result = oblako.status()
    assert result["bedrock"] == "stopped"
    assert result["redshift"] == "stopped"
    assert result["sagemaker"] == "idle"
    assert "moto" not in result  # infra is hidden from status
