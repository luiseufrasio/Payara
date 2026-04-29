"""
Deployment Group Deployment & Redeployment Tests for Payara

Tests that an application deployed to a deployment group is actually
deployed to every instance in that group, and that redeployment also
propagates to all instances.

The test suite automatically:
- Logs all operations with detailed output
- Uses clusterjsp.war from the test-apps folder

Usage:
    export PAYARA_HOME=/path/to/payara
    pytest test_deployment_group_deployment.py -v

To run specific tests:
    pytest test_deployment_group_deployment.py::TestDeploymentGroupDeployment::test_deploy_to_group_appears_on_all_instances -v

Requirements:
    - Payara Server running with DAS accessible
    - PAYARA_HOME environment variable set to Payara installation directory
    - clusterjsp.war file in the test-apps folder
"""

import os
import re
import subprocess
import time
import pytest
import logging
import requests
import socket

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class AsadminRunner:
    """Thin wrapper around asadmin subprocess calls."""

    def __init__(self):
        payara_home = os.environ.get("PAYARA_HOME")
        if not payara_home:
            raise RuntimeError(
                "PAYARA_HOME environment variable is not set. "
                "Please set it to your Payara installation directory."
            )
        self.asadmin = os.path.join(payara_home, "bin", "asadmin")

    def run(self, *args, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.asadmin] + list(args)
        logger.info(f"Running asadmin command: {' '.join(args)}")
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
        )
        if check and result.returncode != 0:
            logger.error(f"asadmin command failed: {' '.join(args)}")
            logger.error(f"stdout: {result.stdout}")
            logger.error(f"stderr: {result.stderr}")
            raise RuntimeError(
                f"asadmin command failed: {' '.join(args)}\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )
        logger.info(f"asadmin command succeeded: {' '.join(args)}")
        return result

    def run_no_raise(self, *args) -> subprocess.CompletedProcess:
        """Run without raising on non-zero exit (useful for cleanup)."""
        return self.run(*args, check=False)

    def list_applications_on_instance(self, instance_name: str) -> list[str]:
        """Return names of applications deployed on the given instance."""
        result = self.run("list-applications", "--long=false", instance_name)
        apps = []
        for line in result.stdout.splitlines():
            line = line.strip()
            # Skip empty lines, the summary line and status messages
            if not line or line.startswith("Command") or line.startswith("Nothing"):
                continue
            # Output format: "<app-name>  <type>"
            parts = line.split()
            if parts:
                apps.append(parts[0])
        return apps

    def get_instance_http_port(self, instance_name: str, instance_ports: dict = None) -> str | None:
        """Get the HTTP listener port for the given instance via get command or from mapping."""
        if instance_ports and instance_name in instance_ports:
            return str(instance_ports[instance_name])
        result = self.run_no_raise(
            "get",
            f"servers.server.{instance_name}.system-property.HTTP_LISTENER_PORT.value",
        )
        for line in result.stdout.splitlines():
            m = re.search(r"=\s*(\d+)", line)
            if m:
                return m.group(1)
        return None


def check_http_app_available(host: str, port: str, app_name: str, timeout: int = 60) -> bool:
    """
    Check if the application is available via HTTP, with retries.
    
    Args:
        host: Hostname or IP address
        port: HTTP port number
        app_name: Name of the application
        timeout: Timeout in seconds for the HTTP request to succeed
    
    Returns:
        True if the application responds with HTTP 200, False otherwise
    """
    url = f"http://{host}:{port}/{app_name}"
    logger.info(f"Checking HTTP availability: {url} (up to {timeout}s)")

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                logger.info(f"✓ Application '{app_name}' is accessible via HTTP at {url}")
                return True
            else:
                logger.debug(f"Application '{app_name}' returned status {response.status_code}, retrying...")
        except requests.exceptions.RequestException as e:
            logger.debug(f"HTTP request failed for {url}: {e}")

        time.sleep(1)

    logger.error(f"Timed out waiting for HTTP availability at {url}")
    return False

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def payara_domain():
    """
    Start the Payara domain before all tests and stop it after.
    """
    asadmin = AsadminRunner()

    # Log PAYARA_HOME
    payara_home = os.environ.get("PAYARA_HOME")
    logger.info(f"PAYARA_HOME: {payara_home}")

    # Log JDK version
    logger.info("Checking JDK version")
    java_result = subprocess.run(["java", "-version"], capture_output=True, text=True)
    logger.info(f"JDK version: {java_result.stderr.strip()}")


@pytest.fixture(scope="module")
def asadmin() -> AsadminRunner:
    return AsadminRunner()


@pytest.fixture(scope="module")
def test_war(tmp_path_factory) -> str:
    """
    Path to a test WAR file. Uses clusterjsp.war from test-apps folder.
    """
    war_path = os.path.join(os.path.dirname(__file__), "..", "test-apps", "clusterjsp.war")
    if not os.path.isfile(war_path):
        raise RuntimeError(f"Test WAR file not found: {war_path}")
    logger.info(f"Using test WAR: {war_path}")
    return war_path


@pytest.fixture()
def deployment_group_env(asadmin):
    """
    Create a deployment group with two standalone instances, yield the
    environment dict, then clean up everything in reverse order.

    Yielded dict keys:
        dg_name       – deployment group name
        instances     – list of instance names
        node_name     – "localhost-domain1" (default local node)
    """
    dg_name = "test-dg"
    node_name = "localhost-test-domain"
    instance_names = ["test-inst1", "test-inst2"]
    # Use fixed ports to ensure HTTP accessibility
    instance_ports = {"test-inst1": 28080, "test-inst2": 28081}


@pytest.fixture()
def single_instance_deployment_group_env(asadmin):
    """
    Create a deployment group with a single standalone instance, yield the
    environment dict, then clean up everything in reverse order.

    Yielded dict keys:
        dg_name       – deployment group name
        instance      – instance name
        instance_port – HTTP port for the instance
        node_name     – "localhost-domain1" (default local node)
    """
    dg_name = "test-dg-single"
    node_name = "localhost-test-domain"
    instance_name = "test-inst-single"
    instance_port = 28090

    logger.info(f"Setting up single instance deployment group environment: {dg_name}")

    logger.info("Checking for and cleaning up any existing resources from previous runs")

    # Clean up any test applications at domain level
    logger.info("Cleaning up test applications from domain level")
    result = asadmin.run_no_raise("list-applications", "--long")
    test_app_prefix = "clusterjsp-dg-"
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Command") or line.startswith("Nothing"):
            continue
        parts = line.split()
        if parts and parts[0].startswith(test_app_prefix):
            app_name = parts[0]
            logger.info(f"Undeploying test application from all targets: {app_name}")
            asadmin.run_no_raise("undeploy", app_name)

    # Stop and delete deployment group if it exists
    asadmin.run_no_raise("stop-deployment-group", dg_name)
    asadmin.run_no_raise("delete-deployment-group", dg_name)

    # Stop and delete instance if it exists
    asadmin.run_no_raise("stop-instance", instance_name)
    asadmin.run_no_raise("delete-instance", instance_name)

    logger.info(f"Creating instance: {instance_name} with HTTP port {instance_port}")
    asadmin.run(
        "create-instance",
        f"--node={node_name}",
        f"--systemproperties=HTTP_LISTENER_PORT={instance_port}",
        instance_name,
    )

    logger.info(f"Creating deployment group: {dg_name}")
    asadmin.run("create-deployment-group", dg_name)

    logger.info(f"Adding instance {instance_name} to deployment group {dg_name}")
    asadmin.run(
        "add-instance-to-deployment-group",
        f"--instance={instance_name}",
        f"--deploymentgroup={dg_name}",
    )

    logger.info(f"Starting deployment group: {dg_name}")
    asadmin.run("start-deployment-group", dg_name)

    logger.info("Waiting for instance to start...")
    time.sleep(10)

    logger.info(f"Single instance deployment group environment setup complete: {dg_name}")

    yield {
        "dg_name": dg_name,
        "instance": instance_name,
        "instance_port": instance_port,
        "node_name": node_name,
    }

    logger.info(f"Tearing down single instance deployment group environment: {dg_name}")

    apps = asadmin.list_applications_on_instance(dg_name)
    if apps:
        logger.info(f"Undeploying remaining apps from {dg_name}: {apps}")
        for app in apps:
            asadmin.run_no_raise("undeploy", f"--target={dg_name}", app)

    # Clean up any test applications at domain level
    logger.info("Cleaning up test applications from domain level")
    result = asadmin.run_no_raise("list-applications", "--long")
    test_app_prefix = "clusterjsp-dg-"
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Command") or line.startswith("Nothing"):
            continue
        parts = line.split()
        if parts and parts[0].startswith(test_app_prefix):
            app_name = parts[0]
            logger.info(f"Undeploying test application from domain: {app_name}")
            asadmin.run_no_raise("undeploy", "--target=domain", app_name)

    logger.info(f"Stopping deployment group: {dg_name}")
    asadmin.run_no_raise("stop-deployment-group", dg_name)

    logger.info(f"Removing instance {instance_name} from deployment group {dg_name}")
    asadmin.run_no_raise("remove-instance-from-deployment-group",
                         f"--instance={instance_name}",
                         f"--deploymentgroup={dg_name}")
    logger.info(f"Deleting instance: {instance_name}")
    asadmin.run_no_raise("delete-instance", instance_name)

    logger.info(f"Deleting deployment group: {dg_name}")
    asadmin.run_no_raise("delete-deployment-group", dg_name)
    logger.info(f"Single instance deployment group environment teardown complete: {dg_name}")


@pytest.fixture()
def deployment_group_env(asadmin):
    """
    Create a deployment group with two standalone instances, yield the
    environment dict, then clean up everything in reverse order.

    Yielded dict keys:
        dg_name       – deployment group name
        instances     – list of instance names
        node_name     – "localhost-domain1" (default local node)
    """
    dg_name = "test-dg"
    node_name = "localhost-test-domain"
    instance_names = ["test-inst1", "test-inst2"]
    # Use fixed ports to ensure HTTP accessibility
    instance_ports = {"test-inst1": 28080, "test-inst2": 28081}

    logger.info(f"Setting up deployment group environment: {dg_name}")

    # --- Pre-setup cleanup (handle stale resources from previous runs) ---
    logger.info("Checking for and cleaning up any existing resources from previous runs")

    # Clean up any test applications at domain level
    logger.info("Cleaning up test applications from domain level")
    result = asadmin.run_no_raise("list-applications", "--long")
    test_app_prefix = "clusterjsp-dg-"
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Command") or line.startswith("Nothing"):
            continue
        parts = line.split()
        if parts and parts[0].startswith(test_app_prefix):
            app_name = parts[0]
            logger.info(f"Undeploying test application from all targets: {app_name}")
            asadmin.run_no_raise("undeploy", app_name)

    # Stop and delete deployment group if it exists
    asadmin.run_no_raise("stop-deployment-group", dg_name)
    asadmin.run_no_raise("delete-deployment-group", dg_name)

    for inst in instance_names:
        logger.info(f"Creating instance: {inst} with HTTP port {instance_ports[inst]}")
        asadmin.run(
            "create-instance",
            f"--node={node_name}",
            f"--systemproperties=HTTP_LISTENER_PORT={instance_ports[inst]}",
            inst,
        )

    logger.info(f"Creating deployment group: {dg_name}")
    asadmin.run("create-deployment-group", dg_name)

    for inst in instance_names:
        logger.info(f"Adding instance {inst} to deployment group {dg_name}")
        asadmin.run(
            "add-instance-to-deployment-group",
            f"--instance={inst}",
            f"--deploymentgroup={dg_name}",
        )

    logger.info(f"Starting deployment group: {dg_name}")
    asadmin.run("start-deployment-group", dg_name)

    logger.info("Waiting for instances to start...")

    logger.info(f"Deployment group environment setup complete: {dg_name}")

    yield {
        "dg_name": dg_name,
        "instances": instance_names,
        "instance_ports": instance_ports,
        "node_name": node_name,
    }

    logger.info(f"Tearing down deployment group environment: {dg_name}")

    apps = asadmin.list_applications_on_instance(dg_name)
    if apps:
        logger.info(f"Undeploying remaining apps from {dg_name}: {apps}")
        for app in apps:
            asadmin.run_no_raise("undeploy", f"--target={dg_name}", app)

    # Clean up any test applications at domain level
    logger.info("Cleaning up test applications from domain level")
    result = asadmin.run_no_raise("list-applications", "--long")
    test_app_prefix = "clusterjsp-dg-"
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Command") or line.startswith("Nothing"):
            continue
        parts = line.split()
        if parts and parts[0].startswith(test_app_prefix):
            app_name = parts[0]
            logger.info(f"Undeploying test application from domain: {app_name}")
            asadmin.run_no_raise("undeploy", "--target=domain", app_name)

    logger.info(f"Stopping deployment group: {dg_name}")
    asadmin.run_no_raise("stop-deployment-group", dg_name)

    for inst in instance_names:
        logger.info(f"Removing instance {inst} from deployment group {dg_name}")
        asadmin.run_no_raise("remove-instance-from-deployment-group",
                             f"--instance={inst}",
                             f"--deploymentgroup={dg_name}")
        logger.info(f"Deleting instance: {inst}")
        asadmin.run_no_raise("delete-instance", inst)

    logger.info(f"Deleting deployment group: {dg_name}")
    asadmin.run_no_raise("delete-deployment-group", dg_name)
    logger.info(f"Deployment group environment teardown complete: {dg_name}")

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeploymentGroupDeployment:
    """Tests for deployment group functionality."""

    def test_deploy_to_group_appears_on_all_instances(
            self, asadmin, deployment_group_env, test_war
    ):
        """Deploying to a deployment group should make the app visible on all instances."""
        dg = deployment_group_env["dg_name"]
        instances = deployment_group_env["instances"]
        instance_ports = deployment_group_env["instance_ports"]
        app_name = "clusterjsp-dg-deploy-test"

        asadmin.run("deploy", f"--target={dg}", f"--name={app_name}", f"--contextroot={app_name}", test_war)

        # Wait for application to start on instances
        logger.info("Waiting for application to start on instances...")
        time.sleep(10)

        try:
            for inst in instances:
                apps = asadmin.list_applications_on_instance(inst)
                assert app_name in apps, (
                    f"Application '{app_name}' not found on instance '{inst}'. Apps: {apps}"
                )

                http_port = asadmin.get_instance_http_port(inst, instance_ports)
                assert http_port is not None, f"Could not get HTTP port for '{inst}'"
                assert check_http_app_available("localhost", http_port, app_name), (
                    f"App '{app_name}' not accessible via HTTP on '{inst}'"
                )
        finally:
            asadmin.run_no_raise("undeploy", f"--target={dg}", app_name)

    def test_deploy_to_group_listed_on_group_target(
            self, asadmin, deployment_group_env, test_war
    ):
        """list-applications on the deployment group should return the deployed app."""
        dg = deployment_group_env["dg_name"]
        instance_ports = deployment_group_env["instance_ports"]
        app_name = "clusterjsp-dg-list-test"

        asadmin.run("deploy", f"--target={dg}", f"--name={app_name}", f"--contextroot={app_name}", test_war)

        # Wait for application to start on instances
        logger.info("Waiting for application to start on instances...")
        time.sleep(10)

        try:
            apps = asadmin.list_applications_on_instance(dg)
            assert app_name in apps, (
                f"App '{app_name}' not listed on deployment group '{dg}'. Apps: {apps}"
            )

            inst = deployment_group_env["instances"][0]
            http_port = asadmin.get_instance_http_port(inst, instance_ports)
            assert http_port is not None, f"Could not get HTTP port for '{inst}'"
            assert check_http_app_available("localhost", http_port, app_name), (
                f"App '{app_name}' not accessible via HTTP on '{inst}'"
            )
        finally:
            asadmin.run_no_raise("undeploy", f"--target={dg}", app_name)

    def test_undeploy_from_group_removes_from_all_instances(
            self, asadmin, deployment_group_env, test_war
    ):
        """Undeploying from a deployment group should remove the app from all instances."""
        dg = deployment_group_env["dg_name"]
        instances = deployment_group_env["instances"]
        instance_ports = deployment_group_env["instance_ports"]
        app_name = "clusterjsp-dg-undeploy-test"

        asadmin.run("deploy", f"--target={dg}", f"--name={app_name}", f"--contextroot={app_name}", test_war)

        # Wait for application to start on instances
        logger.info("Waiting for application to start on instances...")
        time.sleep(10)

        inst = instances[0]
        http_port = asadmin.get_instance_http_port(inst, instance_ports)
        assert http_port is not None, f"Could not get HTTP port for '{inst}'"
        assert check_http_app_available("localhost", http_port, app_name), (
            f"App '{app_name}' not accessible before undeploy"
        )

        asadmin.run("undeploy", f"--target={dg}", app_name)

        for inst in instances:
            apps = asadmin.list_applications_on_instance(inst)
            assert app_name not in apps, (
                f"App '{app_name}' still on instance '{inst}' after undeploy"
            )

    def test_redeploy_to_group_propagates_to_all_instances(
            self, asadmin, deployment_group_env, test_war
    ):
        """Redeploying an app to a deployment group should propagate to all instances."""
        dg = deployment_group_env["dg_name"]
        instances = deployment_group_env["instances"]
        instance_ports = deployment_group_env["instance_ports"]
        app_name = "clusterjsp-dg-redeploy-test"

        # Initial deployment
        asadmin.run("deploy", f"--target={dg}", f"--name={app_name}", f"--contextroot={app_name}", test_war)

        # Wait for application to start on instances
        logger.info("Waiting for application to start on instances...")
        time.sleep(10)

        try:
            # Redeploy the same app with force flag
            asadmin.run("deploy", "--force=true", f"--target={dg}", f"--name={app_name}", f"--contextroot={app_name}",
                        test_war)

            # Verify redeployment propagated to all instances
            for inst in instances:
                apps = asadmin.list_applications_on_instance(inst)
                assert app_name in apps, (
                    f"App '{app_name}' not found on instance '{inst}' after redeploy. Apps: {apps}"
                )

                http_port = asadmin.get_instance_http_port(inst, instance_ports)
                assert http_port is not None, f"Could not get HTTP port for '{inst}'"
                assert check_http_app_available("localhost", http_port, app_name), (
                    f"App '{app_name}' not accessible via HTTP on '{inst}' after redeploy"
                )
        finally:
            asadmin.run_no_raise("undeploy", f"--target={dg}", app_name)

    def test_deploy_redeploy_to_offline_instance(
            self, asadmin, deployment_group_env, test_war
    ):
        """Test that applications are deployed/redeployed to an instance that was offline when the deployment/redeployment took place."""
        dg = deployment_group_env["dg_name"]
        instances = deployment_group_env["instances"]
        instance_ports = deployment_group_env["instance_ports"]
        app_name = "clusterjsp-dg-offline-test"

        # Use the second instance as the one to be taken offline
        offline_inst = instances[1]
        online_inst = instances[0]

        logger.info(f"Stopping instance {offline_inst} to test offline deployment")
        asadmin.run("stop-instance", offline_inst)
        time.sleep(5)

        try:
            # Deploy to the deployment group while one instance is offline
            logger.info(f"Deploying app {app_name} to deployment group {dg} while {offline_inst} is offline")
            asadmin.run("deploy", f"--target={dg}", f"--name={app_name}", f"--contextroot={app_name}", test_war)

            # Wait for application to start on the online instance
            logger.info("Waiting for application to start on online instance...")
            time.sleep(10)

            # Verify app is on the online instance
            apps = asadmin.list_applications_on_instance(online_inst)
            assert app_name in apps, (
                f"Application '{app_name}' not found on online instance '{online_inst}'. Apps: {apps}"
            )

            http_port = asadmin.get_instance_http_port(online_inst, instance_ports)
            assert http_port is not None, f"Could not get HTTP port for '{online_inst}'"
            assert check_http_app_available("localhost", http_port, app_name), (
                f"App '{app_name}' not accessible via HTTP on online instance '{online_inst}'"
            )

            # Start the offline instance
            logger.info(f"Starting offline instance {offline_inst}")
            asadmin.run("start-instance", offline_inst)
            time.sleep(10)

            # Verify the app gets deployed to the previously offline instance
            logger.info(f"Verifying app {app_name} is deployed to previously offline instance {offline_inst}")
            apps = asadmin.list_applications_on_instance(offline_inst)
            assert app_name in apps, (
                f"Application '{app_name}' not found on previously offline instance '{offline_inst}'. Apps: {apps}"
            )

            http_port = asadmin.get_instance_http_port(offline_inst, instance_ports)
            assert http_port is not None, f"Could not get HTTP port for '{offline_inst}'"
            assert check_http_app_available("localhost", http_port, app_name), (
                f"App '{app_name}' not accessible via HTTP on previously offline instance '{offline_inst}'"
            )

            # Now test redeployment scenario: stop the instance again
            logger.info(f"Stopping instance {offline_inst} again to test offline redeployment")
            asadmin.run("stop-instance", offline_inst)
            time.sleep(5)

            # Redeploy the app while instance is offline
            logger.info(f"Redeploying app {app_name} to deployment group {dg} while {offline_inst} is offline")
            asadmin.run("deploy", "--force=true", f"--target={dg}", f"--name={app_name}", f"--contextroot={app_name}",
                        test_war)

            # Wait for redeployment on online instance
            logger.info("Waiting for redeployment on online instance...")
            time.sleep(10)

            # Verify app is still on the online instance after redeploy
            apps = asadmin.list_applications_on_instance(online_inst)
            assert app_name in apps, (
                f"Application '{app_name}' not found on online instance '{online_inst}' after redeploy. Apps: {apps}"
            )

            http_port = asadmin.get_instance_http_port(online_inst, instance_ports)
            assert http_port is not None, f"Could not get HTTP port for '{online_inst}'"
            assert check_http_app_available("localhost", http_port, app_name), (
                f"App '{app_name}' not accessible via HTTP on online instance '{online_inst}' after redeploy"
            )

            # Start the offline instance again
            logger.info(f"Starting offline instance {offline_inst} after redeployment")
            asadmin.run("start-instance", offline_inst)
            time.sleep(10)

            # Verify the redeployed app gets deployed to the previously offline instance
            logger.info(f"Verifying redeployed app {app_name} is deployed to previously offline instance {offline_inst}")
            apps = asadmin.list_applications_on_instance(offline_inst)
            assert app_name in apps, (
                f"Application '{app_name}' not found on previously offline instance '{offline_inst}' after redeploy. Apps: {apps}"
            )

            http_port = asadmin.get_instance_http_port(offline_inst, instance_ports)
            assert http_port is not None, f"Could not get HTTP port for '{offline_inst}'"
            assert check_http_app_available("localhost", http_port, app_name), (
                f"App '{app_name}' not accessible via HTTP on previously offline instance '{offline_inst}' after redeploy"
            )

        finally:
            # Ensure the offline instance is started before cleanup
            logger.info(f"Ensuring instance {offline_inst} is started before cleanup")
            asadmin.run_no_raise("start-instance", offline_inst)
            time.sleep(5)
            asadmin.run_no_raise("undeploy", f"--target={dg}", app_name)

    def test_deploy_then_add_instance_propagates_app(
            self, asadmin, deployment_group_env, test_war
    ):
        """Test that applications deployed to a deployment group are propagated to a running instance when added to the group after deployment."""
        dg = deployment_group_env["dg_name"]
        instances = deployment_group_env["instances"]
        instance_ports = deployment_group_env["instance_ports"]
        node_name = deployment_group_env["node_name"]
        app_name = "clusterjsp-dg-add-instance-test"

        # Use only the first instance for initial deployment
        initial_inst = instances[0]

        # Remove the second instance from the deployment group temporarily
        logger.info(f"Removing instance {instances[1]} from deployment group {dg}")
        asadmin.run(
            "remove-instance-from-deployment-group",
            f"--instance={instances[1]}",
            f"--deploymentgroup={dg}",
        )

        try:
            # Deploy application to deployment group (only has one instance now)
            logger.info(f"Deploying app {app_name} to deployment group {dg}")
            asadmin.run("deploy", f"--target={dg}", f"--name={app_name}", f"--contextroot={app_name}", test_war)

            # Wait for application to start on the initial instance
            logger.info("Waiting for application to start on initial instance...")
            time.sleep(10)

            # Verify app is on the initial instance
            apps = asadmin.list_applications_on_instance(initial_inst)
            assert app_name in apps, (
                f"Application '{app_name}' not found on initial instance '{initial_inst}'. Apps: {apps}"
            )

            http_port = asadmin.get_instance_http_port(initial_inst, instance_ports)
            assert http_port is not None, f"Could not get HTTP port for '{initial_inst}'"
            assert check_http_app_available("localhost", http_port, app_name), (
                f"App '{app_name}' not accessible via HTTP on initial instance '{initial_inst}'"
            )

            # Create a new instance
            new_inst = "test-inst-new"
            new_port = 28082
            logger.info(f"Creating new instance: {new_inst} with HTTP port {new_port}")
            asadmin.run(
                "create-instance",
                f"--node={node_name}",
                f"--systemproperties=HTTP_LISTENER_PORT={new_port}",
                new_inst,
            )

            try:
                # Start the new instance
                logger.info(f"Starting new instance: {new_inst}")
                asadmin.run("start-instance", new_inst)
                time.sleep(10)

                # Add the new instance to the deployment group
                logger.info(f"Adding instance {new_inst} to deployment group {dg}")
                asadmin.run(
                    "add-instance-to-deployment-group",
                    f"--instance={new_inst}",
                    f"--deploymentgroup={dg}",
                )

                # Wait for the application to propagate to the new instance
                logger.info("Waiting for application to propagate to newly added instance...")
                time.sleep(15)

                # Verify the app is deployed to the newly added instance
                apps = asadmin.list_applications_on_instance(new_inst)
                assert app_name in apps, (
                    f"Application '{app_name}' not found on newly added instance '{new_inst}'. Apps: {apps}"
                )

                new_instance_ports = {new_inst: new_port}
                http_port = asadmin.get_instance_http_port(new_inst, new_instance_ports)
                assert http_port is not None, f"Could not get HTTP port for '{new_inst}'"
                assert check_http_app_available("localhost", http_port, app_name), (
                    f"App '{app_name}' not accessible via HTTP on newly added instance '{new_inst}'"
                )

            finally:
                # Cleanup the new instance
                logger.info(f"Removing instance {new_inst} from deployment group {dg}")
                asadmin.run_no_raise(
                    "remove-instance-from-deployment-group",
                    f"--instance={new_inst}",
                    f"--deploymentgroup={dg}",
                )
                logger.info(f"Stopping instance: {new_inst}")
                asadmin.run_no_raise("stop-instance", new_inst)
                time.sleep(5)
                logger.info(f"Deleting instance: {new_inst}")
                asadmin.run_no_raise("delete-instance", new_inst)

        finally:
            # Re-add the second instance to the deployment group for cleanup
            logger.info(f"Re-adding instance {instances[1]} to deployment group {dg}")
            asadmin.run_no_raise(
                "add-instance-to-deployment-group",
                f"--instance={instances[1]}",
                f"--deploymentgroup={dg}",
            )
            asadmin.run_no_raise("undeploy", f"--target={dg}", app_name)

    def test_versioned_deployment_undeploy_old_version_keeps_new_accessible(
            self, asadmin, deployment_group_env, test_war
    ):
        """
        Test that undeploying an old version of a versioned deployment does not break
        access to the newer version when both share the same context root.

        Reproduces bug: After deploying sample-app:1.0.0 and sample-app:1.0.1 with the
        same context root, undeploying sample-app:1.0.0 causes the app to return HTTP 503,
        even though sample-app:1.0.1 is still deployed and enabled.
        """
        dg = deployment_group_env["dg_name"]
        instances = deployment_group_env["instances"]
        instance_ports = deployment_group_env["instance_ports"]
        app_base_name = "sample-app"
        context_root = "sample-app"
        app_v1 = f"{app_base_name}:1.0.0"
        app_v2 = f"{app_base_name}:1.0.1"

        try:
            # Deploy version 1.0.0
            logger.info(f"Deploying {app_v1} with context root {context_root}")
            asadmin.run("deploy", f"--target={dg}", f"--name={app_v1}", f"--contextroot={context_root}", test_war)

            # Wait for application to start
            logger.info("Waiting for v1.0.0 to start...")
            time.sleep(10)

            # Verify v1.0.0 is accessible
            inst = instances[0]
            http_port = asadmin.get_instance_http_port(inst, instance_ports)
            assert http_port is not None, f"Could not get HTTP port for '{inst}'"
            assert check_http_app_available("localhost", http_port, context_root), (
                f"App v1.0.0 not accessible via HTTP on '{inst}'"
            )
            logger.info(f"✓ {app_v1} is accessible via HTTP")

            # Deploy version 1.0.1 (same context root)
            logger.info(f"Deploying {app_v2} with context root {context_root}")
            asadmin.run("deploy", f"--target={dg}", f"--name={app_v2}", f"--contextroot={context_root}", test_war)

            # Wait for application to start
            logger.info("Waiting for v1.0.1 to start...")
            time.sleep(10)

            # Verify v1.0.1 is accessible (should now be the active version)
            assert check_http_app_available("localhost", http_port, context_root), (
                f"App v1.0.1 not accessible via HTTP on '{inst}' after deployment"
            )
            logger.info(f"✓ {app_v2} is accessible via HTTP")

            # Undeploy the old version (1.0.0)
            logger.info(f"Undeploying old version {app_v1}")
            asadmin.run("undeploy", f"--target={dg}", app_v1)

            # Wait for undeployment to complete
            time.sleep(5)

            # Verify v1.0.1 is still accessible after v1.0.0 is removed
            # This is the bug - currently returns HTTP 503
            assert check_http_app_available("localhost", http_port, context_root), (
                f"App v1.0.1 not accessible via HTTP on '{inst}' after undeploying v1.0.0. "
                f"This is the bug - the newer version should remain accessible."
            )
            logger.info(f"✓ {app_v2} remains accessible after undeploying {app_v1}")

            # Verify v1.0.1 is still listed as deployed
            apps = asadmin.list_applications_on_instance(dg)
            assert app_v2 in apps, (
                f"App v1.0.1 not listed on deployment group '{dg}' after undeploying v1.0.0. Apps: {apps}"
            )
            logger.info(f"✓ {app_v2} is still listed as deployed")

        finally:
            # Cleanup both versions
            logger.info(f"Cleaning up versioned deployments")
            asadmin.run_no_raise("undeploy", f"--target={dg}", app_v1)
            asadmin.run_no_raise("undeploy", f"--target={dg}", app_v2)

    def test_redeploy_breaks_virtual_server_default_module(
            self, asadmin, single_instance_deployment_group_env, test_war
    ):
        """
        Test that redeploying an application to a deployment group breaks the virtual server
        when the application is used as the Default Web Module.

        Reproduces bug: After setting a deployed app as the Default Web Module for an instance's
        virtual server, redeploying the app with --force=true causes the virtual server endpoint
        to fail with an exception in the server log.
        """
        dg = single_instance_deployment_group_env["dg_name"]
        inst = single_instance_deployment_group_env["instance"]
        http_port = str(single_instance_deployment_group_env["instance_port"])
        app_name = "clusterjsp-dg-default-module-test"
        context_root = "/"

        virtual_server = "server"

        try:
            logger.info(f"Deploying {app_name} to deployment group {dg}")
            asadmin.run("deploy", f"--target={dg}", f"--name={app_name}", f"--contextroot={context_root}", test_war)

            logger.info("Waiting for application to start...")
            time.sleep(10)

            root_url = f"http://localhost:{http_port}/"
            try:
                response = requests.get(root_url, timeout=5)
                assert response.status_code == 200, (
                    f"App '{app_name}' not accessible via HTTP on '{inst}' at root URL (status: {response.status_code})"
                )
                logger.info(f"✓ {app_name} is accessible via root URL")
            except requests.exceptions.RequestException as e:
                pytest.fail(f"App '{app_name}' not accessible via HTTP on '{inst}' at root URL: {e}")

            logger.info(f"Redeploying {app_name} to deployment group {dg} with --force=true")
            asadmin.run("deploy", "--force=true", f"--target={dg}", f"--name={app_name}", f"--contextroot={context_root}",
                        test_war)

            logger.info("Waiting for redeployment to complete...")
            time.sleep(10)

            logger.info(f"Testing virtual server endpoint after redeploy: {root_url}")
            try:
                response = requests.get(root_url, timeout=5)
                assert response.status_code == 200, (
                    f"Virtual server endpoint returned status {response.status_code} after redeploy. "
                    f"This indicates the bug is not fixed."
                )
                logger.info(f"✓ Virtual server endpoint is accessible after redeploy (status: {response.status_code})")
            except requests.exceptions.RequestException as e:
                pytest.fail(f"Virtual server endpoint failed after redeploy: {e}. This indicates the bug is not fixed.")

        finally:
            asadmin.run_no_raise("undeploy", f"--target={dg}", app_name)