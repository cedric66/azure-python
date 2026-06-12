"""Azure Resource Graph client: one paged query covers all subscriptions at once,
which is what keeps a 25-subscription / 500-cluster sweep down to a handful of calls."""

from .http_client import AzureApiError

API = "/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"

CLUSTERS_KQL = """
Resources
| where type =~ 'microsoft.containerservice/managedclusters'
| project id, name, resourceGroup, subscriptionId, location, tags,
    kubernetesVersion = tostring(properties.kubernetesVersion),
    currentKubernetesVersion = tostring(properties.currentKubernetesVersion),
    provisioningState = tostring(properties.provisioningState),
    powerState = tostring(properties.powerState.code),
    skuName = tostring(sku.name), skuTier = tostring(sku.tier),
    supportPlan = tostring(properties.supportPlan),
    nodeResourceGroup = tostring(properties.nodeResourceGroup),
    dnsPrefix = tostring(properties.dnsPrefix),
    fqdn = tostring(properties.fqdn),
    privateFQDN = tostring(properties.privateFQDN),
    enableRBAC = tobool(properties.enableRBAC),
    disableLocalAccounts = tobool(properties.disableLocalAccounts),
    identityType = tostring(identity.type),
    servicePrincipalClientId = tostring(properties.servicePrincipalProfile.clientId),
    agentPoolProfiles = properties.agentPoolProfiles,
    networkProfile = properties.networkProfile,
    apiServerAccessProfile = properties.apiServerAccessProfile,
    aadProfile = properties.aadProfile,
    addonProfiles = properties.addonProfiles,
    autoUpgradeProfile = properties.autoUpgradeProfile,
    securityProfile = properties.securityProfile,
    oidcIssuerProfile = properties.oidcIssuerProfile,
    autoScalerProfile = properties.autoScalerProfile
"""

# Verbatim resource bodies for policy what-if evaluation (sandbox impact): the
# projected CLUSTERS_KQL drops fields, which would skew alias matching.
CLUSTERS_RAW_KQL = """
Resources
| where type =~ 'microsoft.containerservice/managedclusters'
| project id, name, type, location, tags, sku, identity, properties, subscriptionId
"""

RG_TAGS_KQL = """
ResourceContainers
| where type =~ 'microsoft.resources/subscriptions/resourcegroups'
| project subscriptionId, name = tolower(name), tags
"""

SUB_NAMES_KQL = """
ResourceContainers
| where type =~ 'microsoft.resources/subscriptions'
| project subscriptionId, name
"""

SUBNETS_KQL = """
Resources
| where type =~ 'microsoft.network/virtualnetworks'
| extend vnetName = name
| mv-expand subnet = properties.subnets
| project id = tostring(subnet.id),
    name = tostring(subnet.name),
    subscriptionId,
    resourceGroup,
    location,
    vnet = tostring(vnetName),
    addressPrefix = tostring(subnet.properties.addressPrefix),
    addressPrefixes = subnet.properties.addressPrefixes,
    nsgId = tostring(subnet.properties.networkSecurityGroup.id),
    routeTableId = tostring(subnet.properties.routeTable.id),
    natGatewayId = tostring(subnet.properties.natGateway.id),
    privateEndpointNetworkPolicies = tostring(subnet.properties.privateEndpointNetworkPolicies),
    serviceEndpoints = subnet.properties.serviceEndpoints,
    delegations = subnet.properties.delegations
"""


def query(session, kql, subscription_ids, page=1000):
    rows = []
    ids = list(subscription_ids)
    for start in range(0, len(ids), 1000):
        chunk = ids[start:start + 1000]
        skip = None
        while True:
            opts = {"resultFormat": "objectArray", "$top": page}
            if skip:
                opts["$skipToken"] = skip
            try:
                data = session.post(API, payload={"subscriptions": chunk, "query": kql,
                                                  "options": opts})
            except AzureApiError as e:
                raise AzureApiError(
                    "Resource Graph query failed (%d subscription(s)): %s\n"
                    "--- KQL sent to Azure ---\n%s\n-------------------------"
                    % (len(chunk), e, kql.strip()), e.status, e.body) from e
            rows.extend(data.get("data") or [])
            skip = data.get("$skipToken")
            if not skip:
                break
    return rows
