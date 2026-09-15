import { apiFetch, parseApiError } from "./client";
import type { BackgroundJobItem } from "../types";
export async function listJobs():Promise<{total:number;items:BackgroundJobItem[]}>{const r=await apiFetch("/api/system/jobs?limit=100");if(!r.ok)throw await parseApiError(r);return r.json();}
export async function jobAction(id:string,action:"retry"|"cancel"):Promise<BackgroundJobItem>{const r=await apiFetch(`/api/system/jobs/${id}/${action}`,{method:"POST"});if(!r.ok)throw await parseApiError(r);return r.json();}
