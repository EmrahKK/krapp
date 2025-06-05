import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from kubernetes import client, config
from krr import SimpleStrategy, PrometheusMetricsFetcher

app = FastAPI(
    title="Kubernetes Resource Advisor",
    description="KRR tabanlı resource öneri sistemi"
)

# Kubernetes bağlantı konfigürasyonu
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

class RecommendationRequest(BaseModel):
    namespace: str
    workload: str
    workload_type: str = "deployment"  # deployment veya statefulset
    time_window: str = "14d"  # ör: 1d, 48h, 2w

class GapThreshold(BaseModel):
    cpu_threshold: float = 30.0  # %30 varsayılan
    memory_threshold: float = 30.0

@app.get("/namespaces")
def list_namespaces():
    try:
        v1 = client.CoreV1Api()
        namespaces = [ns.metadata.name for ns in v1.list_namespace().items]
        return {"namespaces": namespaces}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/workloads")
def list_workloads(namespace: str):
    try:
        apps_v1 = client.AppsV1Api()
        
        deployments = apps_v1.list_namespaced_deployment(namespace).items
        statefulsets = apps_v1.list_namespaced_stateful_set(namespace).items
        
        workloads = []
        for dep in deployments:
            workloads.append({
                "name": dep.metadata.name,
                "type": "deployment",
                "current_resources": extract_current_resources(dep.spec.template.spec.containers)
            })
            
        for sts in statefulsets:
            workloads.append({
                "name": sts.metadata.name,
                "type": "statefulset",
                "current_resources": extract_current_resources(sts.spec.template.spec.containers)
            })
            
        return {"workloads": workloads}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def extract_current_resources(containers):
    resources = []
    for container in containers:
        container_res = {
            "container": container.name,
            "requests": {},
            "limits": {}
        }
        if container.resources.requests:
            container_res["requests"] = {
                "cpu": container.resources.requests.get("cpu", "0m"),
                "memory": container.resources.requests.get("memory", "0Mi")
            }
        if container.resources.limits:
            container_res["limits"] = {
                "cpu": container.resources.limits.get("cpu", "0m"),
                "memory": container.resources.limits.get("memory", "0Mi")
            }
        resources.append(container_res)
    return resources

@app.post("/recommendations")
def get_recommendations(request: RecommendationRequest):
    try:
        prometheus_url = os.getenv("PROMETHEUS_URL", "http://prometheus-k8s.monitoring:9090")
        fetcher = PrometheusMetricsFetcher(prometheus_url)
        strategy = SimpleStrategy(fetcher)
        
        # KRR'den önerileri al
        result = strategy.calculate(
            namespace=request.namespace,
            pod=request.workload,
            time_window=request.time_window
        )
        
        return {
            "workload": request.workload,
            "namespace": request.namespace,
            "recommendations": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/audit/gaps")
def audit_gaps(threshold: GapThreshold = GapThreshold()):
    try:
        v1_namespaces = client.CoreV1Api().list_namespace()
        apps_v1 = client.AppsV1Api()
        gap_workloads = []
        
        for ns in v1_namespaces.items:
            namespace = ns.metadata.name
            
            # Deployment'ları kontrol et
            deployments = apps_v1.list_namespaced_deployment(namespace).items
            for dep in deployments:
                gap_workloads.extend(
                    check_workload_gap(dep, "deployment", threshold)
                
            # StatefulSet'leri kontrol et
            statefulsets = apps_v1.list_namespaced_stateful_set(namespace).items
            for sts in statefulsets:
                gap_workloads.extend(
                    check_workload_gap(sts, "statefulset", threshold))
        
        return {"workloads_with_gaps": gap_workloads}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def check_workload_gap(workload, workload_type, threshold):
    gap_data = []
    workload_name = workload.metadata.name
    namespace = workload.metadata.namespace
    
    # Önerileri al
    recommendations = get_recommendations(RecommendationRequest(
        namespace=namespace,
        workload=workload_name,
        workload_type=workload_type
    ))
    
    # Mevcut kaynakları çıkar
    current_resources = extract_current_resources(
        workload.spec.template.spec.containers
    )
    
    # Container bazında karşılaştır
    for container in current_resources:
        container_name = container["container"]
        current_cpu = container["requests"].get("cpu", "0m")
        current_mem = container["requests"].get("memory", "0Mi")
        
        # Önerilen kaynakları bul
        recommended = next(
            (rec for rec in recommendations["recommendations"] 
             if rec["container"] == container_name), None
        )
        
        if not recommended:
            continue
            
        # CPU farkını hesapla
        cpu_gap = calculate_resource_gap(
            current_cpu, 
            recommended["recommended"]["requests"]["cpu"],
            threshold.cpu_threshold
        )
        
        # Memory farkını hesapla
        mem_gap = calculate_resource_gap(
            current_mem, 
            recommended["recommended"]["requests"]["memory"],
            threshold.memory_threshold
        )
        
        if cpu_gap["exceeds"] or mem_gap["exceeds"]:
            gap_data.append({
                "namespace": namespace,
                "workload": workload_name,
                "type": workload_type,
                "container": container_name,
                "current_cpu": current_cpu,
                "recommended_cpu": recommended["recommended"]["requests"]["cpu"],
                "cpu_gap_percent": cpu_gap["gap_percent"],
                "current_memory": current_mem,
                "recommended_memory": recommended["recommended"]["requests"]["memory"],
                "memory_gap_percent": mem_gap["gap_percent"]
            })
    
    return gap_data

def calculate_resource_gap(current, recommended, threshold):
    # Kaynakları milicore/megabyte'a çevir
    def parse_resource(res):
        if res.endswith("m"):
            return float(res[:-1])
        elif res.endswith("Mi"):
            return float(res[:-2])
        return float(res)
    
    current_val = parse_resource(current)
    recommended_val = parse_resource(recommended)
    
    # Fark yüzdesini hesapla
    gap_percent = abs((current_val - recommended_val) / current_val * 100) if current_val != 0 else 0
    
    return {
        "gap_percent": gap_percent,
        "exceeds": gap_percent > threshold
    }