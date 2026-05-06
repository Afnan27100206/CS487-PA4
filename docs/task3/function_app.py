# azf + df
import azure.functions as func
import azure.durable_functions as df
import os
import time
import requests
import json
import logging

# mi + aci sdk
from azure.identity import ManagedIdentityCredential
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerinstance.models import (
    ContainerGroup,
    Container,
    ResourceRequirements,
    ResourceRequests,
    ContainerGroupRestartPolicy,
    EnvironmentVariable,
    ImageRegistryCredential,
    OperatingSystemTypes,
    ContainerGroupIdentity,
    ResourceIdentityType,
)

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)


# kicks off orch returns status urls
@app.route(route="orchestrators/{orchestratorName}", methods=["POST"])
@app.durable_client_input(client_name="client")
async def http_starter(req: func.HttpRequest, client):
    orchestrator_name = req.route_params.get("orchestratorName")
    body = req.get_json()
    instance_id = await client.start_new(orchestrator_name, client_input=body)
    logging.info(f"started orch id={instance_id}")
    return client.create_check_status_response(req, instance_id)


# val then report bails if val fails
@app.orchestration_trigger(context_name="context")
def my_orchestrator(context: df.DurableOrchestrationContext):
    order = context.get_input()

    validation = yield context.call_activity("validate_activity", order)

    # early exit skip aci spin
    if not validation.get("valid"):
        return {
            "status": "rejected",
            "reason": validation.get("reason", "unknown")
        }

    report_url = yield context.call_activity("report_activity", order)

    return {
        "status": "completed",
        "report_url": report_url
    }


# va hits aks /validate
@app.activity_trigger(input_name="order")
def validate_activity(order: dict) -> dict:
    validate_url = os.environ["VALIDATE_URL"]  # injected by fa env
    logging.info(f"va oid={order.get('order_id')}")
    response = requests.post(validate_url, json=order, timeout=15)
    response.raise_for_status()
    result = response.json()
    logging.info(f"va result={result}")
    return result

    # # httpx alt async
    # import httpx
    # async with httpx.AsyncClient() as c:
    #     r = await c.post(validate_url, json=order, timeout=15)
    #     return r.json()


# ra spins aci polls cleans up
@app.activity_trigger(input_name="order")
def report_activity(order: dict) -> str:
    sub_id      = os.environ["SUBSCRIPTION_ID"]
    rg          = os.environ["REPORT_RG"]
    location    = os.environ["REPORT_LOCATION"]
    image       = os.environ["REPORT_IMAGE"]
    acr_server  = os.environ["ACR_SERVER"]
    acr_user    = os.environ["ACR_USERNAME"]
    acr_pass    = os.environ["ACR_PASSWORD"]
    storage_url = os.environ["STORAGE_ACCOUNT_URL"]
    client_id   = os.environ["AZURE_CLIENT_ID"]
    order_id    = order["order_id"]

    logging.info(f"ra oid={order_id}")

    credential = ManagedIdentityCredential(client_id=client_id)  # uai scoped
    aci_client = ContainerInstanceManagementClient(credential, sub_id)

    group_name = f"ci-report-{order_id.lower()}"

    # arm id of pre provisioned uai
    mi_resource_id = (
        f"/subscriptions/{sub_id}/resourcegroups/{rg}/providers/"
        f"Microsoft.ManagedIdentity/userAssignedIdentities/mi-pa4-27100206"
    )

    # # sys assigned alt blocked by sub policy so uai instead
    # identity = ContainerGroupIdentity(type=ResourceIdentityType.system_assigned)

    container_group = ContainerGroup(
        location=location,
        os_type=OperatingSystemTypes.linux,
        restart_policy=ContainerGroupRestartPolicy.never,  # one shot
        identity=ContainerGroupIdentity(
            type=ResourceIdentityType.user_assigned,
            user_assigned_identities={mi_resource_id: {}}
        ),
        image_registry_credentials=[
            ImageRegistryCredential(
                server=acr_server,
                username=acr_user,
                password=acr_pass
            )
        ],
        containers=[Container(
            name="report",
            image=image,
            resources=ResourceRequirements(
                requests=ResourceRequests(cpu=1.0, memory_in_gb=1.5)
            ),
            environment_variables=[
                EnvironmentVariable(name="ORDER_ID",            value=order_id),
                EnvironmentVariable(name="ORDER_JSON",          value=json.dumps(order)),
                EnvironmentVariable(name="STORAGE_ACCOUNT_URL", value=storage_url),
                EnvironmentVariable(name="AZURE_CLIENT_ID",     value=client_id),
            ]
        )],
    )

    logging.info(f"creating cg={group_name}")
    aci_client.container_groups.begin_create_or_update(
        rg, group_name, container_group
    ).result()

    # 60x5s = 5min max
    for attempt in range(60):
        time.sleep(5)
        try:
            cg = aci_client.container_groups.get(rg, group_name)
            c = cg.containers[0]
            state = (
                c.instance_view.current_state.state
                if c.instance_view and c.instance_view.current_state
                else None
            )
            logging.info(f"poll {attempt+1}/60 state={state}")
            if state in ("Terminated", "Succeeded"):
                break
        except Exception as poll_err:
            logging.warning(f"poll err={poll_err}")

    # # cg level alt less granular
    # cg_state = cg.instance_view.state if cg.instance_view else None

    try:
        aci_client.container_groups.begin_delete(rg, group_name)
    except Exception as del_err:
        logging.warning(f"del err={del_err}")

    storage_account = storage_url.rstrip("/").split("//")[1].split(".")[0]
    blob_url = f"https://{storage_account}.blob.core.windows.net/reports/{order_id}.pdf"
    logging.info(f"blob_url={blob_url}")
    return blob_url
