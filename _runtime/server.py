#!/usr/bin/env python3
import html, json, os, re, sqlite3
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from workflow_engine import WorkflowEngine, WorkflowError, render_workflow_ui

APP_ROOT = Path(os.environ.get("MERGED_APP_ROOT", os.getcwd()))
DB_PATH = APP_ROOT / "database.sqlite"
MANIFEST = json.loads((APP_ROOT / "manifest.json").read_text())
PLACEHOLDER_ENV_MARKERS = ("your_", "your-", "replace", "placeholder", "changeme", "example", "xxxx")

def load_env_file(path):
    if not path.is_file(): return
    for raw_line in path.read_text(errors="ignore").splitlines():
        line=raw_line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        if line.startswith("export "): line=line[7:].lstrip()
        key,value=line.split("=",1); key=key.strip(); value=value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",key): continue
        if len(value)>=2 and value[0]==value[-1] and value[0] in {'\"',"'"}: value=value[1:-1]
        os.environ.setdefault(key,value)

def load_merged_environment():
    load_env_file(APP_ROOT/".env")

def configured_env(name):
    value=os.environ.get(name,"").strip()
    return value if value and not any(marker in value.lower() for marker in PLACEHOLDER_ENV_MARKERS) else ""

load_merged_environment()
PORT = int(os.environ.get("PORT", "4400"))
HOST = os.environ.get("MERGED_HOST", "127.0.0.1")
WORKFLOW_ENGINE = WorkflowEngine(APP_ROOT, DB_PATH)

def rows(sql, params=()):
    connection = sqlite3.connect(DB_PATH); connection.row_factory = sqlite3.Row
    result = [dict(row) for row in connection.execute(sql, params).fetchall()]
    connection.close(); return result

def execute(sql, params=()):
    connection=sqlite3.connect(DB_PATH); cursor=connection.execute(sql,params); connection.commit(); row_id=cursor.lastrowid; connection.close(); return row_id

def quoted(identifier):
    return '"' + identifier.replace('"', '""') + '"'

def feature_payload(row):
    return {"id":row["id"],"name":row["name"],"canonicalKey":row["canonical_key"],"kinds":json.loads(row["kinds_json"]),"routes":json.loads(row["routes_json"]),"sourceProjects":json.loads(row["source_projects_json"]),"aliases":json.loads(row["aliases_json"]),"consolidatedFromApps":json.loads(row["consolidated_from_apps_json"]),"evidenceCount":row["evidence_count"]}

def ai_binding_is_valid(page):
    """Reject ambiguous AI bindings originating from mixed router files."""
    feature_id=page.get("feature_id")
    if not feature_id: return False
    stem=Path(str(page.get("source_path", ""))).stem.lower()
    if stem not in {"app","routes","router"}: return True
    source_path=str(page.get("source_path", "")); route=str(page.get("route", "")).rstrip("/") or "/"
    evidence=rows("SELECT evidence_path,route FROM feature_evidence WHERE feature_id=? AND source_project=?",(feature_id,page.get("source_project", "")))
    return any(re.sub(r":\d+$","",str(item["evidence_path"]))==source_path and (str(item["route"]).rstrip("/") or "/")==route for item in evidence)

def native_table_payload(physical_table):
    registry = rows("SELECT * FROM seed_table_registry WHERE physical_table=?", (physical_table,))
    if not registry: return None
    columns = rows("SELECT source_column,physical_column,sqlite_type FROM seed_table_columns WHERE table_id=? ORDER BY ordinal", (registry[0]["id"],))
    data = rows(f"SELECT * FROM {quoted(physical_table)}")
    return {"name":physical_table,"sourceProject":registry[0]["source_project"],"sourceTable":registry[0]["source_table"],"columns":columns,"rows":data}

def record_attributes(row):
    payload=html.escape(json.dumps(row,default=str,ensure_ascii=False),quote=True)
    return f'class="record-row" tabindex="0" role="button" aria-label="Open record details" data-record="{payload}"'

def native_table_html(payload):
    headers = "".join(f'<th>{html.escape(column["physical_column"])}</th>' for column in payload["columns"])
    body = "".join(f'<tr {record_attributes(row)}>' + ''.join(f'<td>{html.escape(str(row.get(column["physical_column"], "")))}</td>' for column in payload["columns"]) + '</tr>' for row in payload["rows"])
    return f'''<section class="detail"><div class="eyebrow">Native database table</div><h1>{html.escape(payload["name"])}</h1><p class="lead">Complete table with {len(payload["rows"])} rows. Project and source-table provenance are metadata, not business-table columns.</p><div class="table"><table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></div></section>'''

def feature_table_html(feature, payload, related):
    headers="".join(f'<th>{html.escape(column["physical_column"])}</th>' for column in payload["columns"])
    body="".join(f'<tr {record_attributes(row)}>'+''.join(f'<td>{html.escape(str(row.get(column["physical_column"], "")))}</td>' for column in payload["columns"])+'</tr>' for row in payload["rows"])
    related_links=[f'<a href="/?table={quote(table["physical_table"])}">{html.escape(table["physical_table"])}</a>' for table in related[1:]]
    related_html=f'<p class="lead">Related data: {" · ".join(related_links)}</p>' if related_links else ""
    return f'''<section class="detail"><div class="eyebrow">{html.escape(payload["name"])}</div><h1>{html.escape(feature["name"])}</h1><p class="lead">{len(payload["rows"])} records</p>{related_html}<div class="table"><table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table></div></section>'''

def normalized_name(value):
    return re.sub(r"[^a-z0-9]+","_",str(value).lower()).strip("_")

def singular_name(value):
    value=normalized_name(value)
    if value.endswith("ies") and len(value)>4: return value[:-3]+"y"
    if value.endswith("s") and not value.endswith("ss") and len(value)>3: return value[:-1]
    return value

def field_data_options(page,field,limit=200):
    key=str(field.get("key","")).strip()
    if not key or str(field.get("type","")).lower() in {"password","textarea","json"}: return []
    key_name=normalized_name(key); entity=key_name[:-3] if key_name.endswith("_id") else ""
    metadata=rows("""SELECT seed_table_registry.*,seed_table_columns.source_column,seed_table_columns.physical_column,
        COALESCE(feature_table_links.relevance_score,0) AS relevance_score
        FROM seed_table_columns JOIN seed_table_registry ON seed_table_registry.id=seed_table_columns.table_id
        LEFT JOIN feature_table_links ON feature_table_links.table_id=seed_table_registry.id AND feature_table_links.feature_id=?
        WHERE seed_table_registry.row_count>0""",(page["feature_id"],))
    candidates=[]
    for item in metadata:
        column_name=normalized_name(item["physical_column"]); source_column=normalized_name(item["source_column"])
        table_name=normalized_name(item["source_table"]); physical_table=normalized_name(item["physical_table"])
        exact=column_name==key_name or source_column==key_name
        entity_id=bool(entity and column_name in {"id",key_name} and (entity in table_name or entity in physical_table))
        if not exact and not entity_id: continue
        score=(120 if exact else 80)+min(int(item["relevance_score"] or 0),100)
        if item["source_project"]==page["source_project"]: score+=35
        if entity and singular_name(table_name)==singular_name(entity): score+=250
        candidates.append((score,item))
    if not candidates: return []
    _,chosen=max(candidates,key=lambda pair:(pair[0],pair[1]["row_count"],pair[1]["physical_table"]))
    table_columns=rows("SELECT physical_column FROM seed_table_columns WHERE table_id=? ORDER BY ordinal",(chosen["id"],))
    column_names=[item["physical_column"] for item in table_columns]
    preferred=("name","title","common_name","full_name","customer_name","apiary_name","hive_name","label","location","region","email","status","type","description","scientific_name")
    label_columns=[]
    for preference in preferred:
        match=next((column for column in column_names if normalized_name(column)==preference and column!=chosen["physical_column"]),None)
        if match and match not in label_columns: label_columns.append(match)
        if len(label_columns)==2: break
    data=rows(f"SELECT * FROM {quoted(chosen['physical_table'])} WHERE {quoted(chosen['physical_column'])} IS NOT NULL LIMIT ?",(limit*3,))
    options=[]; seen=set()
    for record in data:
        value=record.get(chosen["physical_column"])
        if value is None or not str(value).strip() or str(value) in seen: continue
        seen.add(str(value)); details=[str(record.get(column,"")) for column in label_columns if record.get(column) not in (None,"")]
        label=str(value)+(f" — {' · '.join(details)}" if details else "")
        options.append({"value":value,"label":label,"table":chosen["physical_table"]})
        if len(options)>=limit: break
    return options

def example_value(field,variant,page):
    key=str(field.get("key","")).lower(); label=str(field.get("label",key)); field_type=str(field.get("type","text")); data_options=field.get("_data_options",[]); options=field.get("options",[])
    default=field.get("defaultValue")
    if default not in (None,""): return default
    if data_options: return data_options[min(variant,len(data_options)-1)]["value"]
    if field_type=="select" and options: return options[min(variant,len(options)-1)]
    if field_type=="number" or any(token in key for token in ("count","weeks","hives","score","rate","amount","quantity","total","horizon")): return (5,12,25)[variant]
    if field_type=="date" or key.endswith("_date") or "week_start" in key: return (date.today()+timedelta(days=(0,7,30)[variant])).isoformat()
    if "captured_at" in key or key.endswith("_at"): return datetime.now().replace(microsecond=0).isoformat()
    if "email" in key: return ("manager@example.com","operations@example.com","review@example.com")[variant]
    if "url" in key: return "https://example.com/reference"
    if key.endswith("_id") or key=="id": return str((1,5,12)[variant])
    choices={
        "season":("Spring","Summer","Autumn"),"customer":("Green Valley Farms","North Ridge Cooperative","Regional Operations Team"),
        "crop":("Almond","Blueberry","Apple"),"product":("Standard treatment","Organic treatment","Integrated treatment"),
        "status":("Active","Review required","High priority"),"type":("Standard","Premium","Specialty"),
    }
    if key in choices: return choices[key][variant]
    placeholder=str(field.get("placeholder","")).strip()
    if placeholder and not placeholder.lower().startswith(("enter ","select ")): return placeholder
    if field_type=="textarea" or any(token in key for token in ("notes","question","narrative","focus","description","constraints","stops","context")):
        return (
            f"Review the current {page['title'].replace('AI · ','').lower()} conditions and recommend the safest practical next steps.",
            f"Analyze recent operational signals, explain the main risks, and provide a prioritized action plan with measurable follow-up steps.",
            f"Evaluate a high-priority scenario with limited resources. State assumptions, flag uncertainties, and include immediate and longer-term actions.",
        )[variant]
    return (f"Sample {label}",f"Detailed {label}",f"Priority {label}")[variant]

def ai_page_html(page):
    inputs=json.loads(page["inputs_json"]); controls=[]; resolved_inputs=[]
    for original_field in inputs:
        data_options=field_data_options(page,original_field); field={**original_field,"_data_options":data_options} if data_options else original_field
        key=str(field.get("key", "")); label=str(field.get("label", key)); field_type=str(field.get("type", "text")); placeholder=str(field.get("placeholder", "")); default=field.get("defaultValue", "")
        if data_options:
            field_type="select"; source_table=data_options[0]["table"]
            options='<option value="">Choose existing data…</option>'+''.join(f'<option value="{html.escape(str(option["value"]))}" {"selected" if str(option["value"])==str(default) else ""}>{html.escape(str(option["label"]))}</option>' for option in data_options)
            control=f'<select name="{html.escape(key)}" data-type="select" data-native-table="{html.escape(source_table)}">{options}</select><small class="field-source">From {html.escape(source_table)} · {len(data_options)} available</small>'
        elif field_type=="select":
            options='<option value="">—</option>'+''.join(f'<option value="{html.escape(str(option))}" {"selected" if option==default else ""}>{html.escape(str(option))}</option>' for option in field.get("options",[]))
            control=f'<select name="{html.escape(key)}" data-type="select">{options}</select>'
        elif field_type=="textarea":
            control=f'<textarea name="{html.escape(key)}" data-type="textarea" placeholder="{html.escape(placeholder)}">{html.escape(str(default))}</textarea>'
        else:
            html_type=field_type if field_type in {"number","date","email","url","password"} else "text"
            control=f'<input name="{html.escape(key)}" data-type="{html.escape(field_type)}" type="{html_type}" value="{html.escape(str(default))}" placeholder="{html.escape(placeholder)}">'
        controls.append(f'<div class="form-group {"full" if field_type=="textarea" else ""}"><label>{html.escape(label)}</label>{control}</div>')
        resolved_inputs.append(field)
    presets=[]
    for variant,label in enumerate(("Quick example","Detailed example","Priority scenario")):
        values={str(field.get("key","")):example_value(field,variant,page) for field in resolved_inputs}
        encoded=html.escape(json.dumps(values,ensure_ascii=False),quote=True)
        presets.append(f'<button type="button" class="preset-button" data-preset="{encoded}">{label}</button>')
    endpoint_note=html.escape(page["endpoint"] or page["endpoint_path"] or "OpenRouter")
    return f'''<section class="detail ai-page"><div class="eyebrow">AI feature</div><h1>{html.escape(page["title"])}</h1><p class="lead">{html.escape(page["subtitle"])}</p><form id="ai-form" data-page-id="{page["id"]}"><div class="preset-bar"><span>Fill every field</span>{"".join(presets)}</div><div class="form-grid">{"".join(controls)}</div><div class="actions"><button class="run-button" type="submit">{html.escape(page["button_label"])}</button></div></form><div id="ai-status" class="status" aria-live="polite"></div><section id="ai-result" class="ai-result" hidden><div class="result-heading"><div><div class="eyebrow">AI analysis</div><h2>Professional recommendation</h2></div><span id="ai-model" class="model-chip"></span></div><div id="ai-result-body" class="result-body"></div></section><details class="binding"><summary>Provider and source binding</summary><code>OpenRouter → {endpoint_note}</code><br><code>{html.escape(page["source_path"])}</code></details></section><script>
    (()=>{{
      const form=document.getElementById('ai-form');if(!form)return;
      for(const preset of form.querySelectorAll('[data-preset]'))preset.addEventListener('click',()=>{{const values=JSON.parse(preset.dataset.preset);let filled=0;for(const field of form.elements){{if(!field.name)continue;let value=values[field.name];if(value===undefined||value===null||String(value).trim()===''){{if(field.tagName==='SELECT')value=Array.from(field.options).find(option=>option.value)?.value||'';else value=`Example ${{field.name.replace(/_/g,' ')}}`;}}field.value=value;if(!String(field.value).trim()&&field.tagName==='SELECT'){{const option=Array.from(field.options).find(item=>item.value);if(option)field.value=option.value;}}if(String(field.value).trim())filled++;field.dispatchEvent(new Event('change',{{bubbles:true}}));}}const status=document.getElementById('ai-status');status.className='status complete';status.textContent=`Filled ${{filled}} of ${{form.querySelectorAll('[name]').length}} fields, including optional fields.`;}});
      const renderProfessional=(text,target)=>{{target.replaceChildren();let list=null;for(const raw of String(text||'').replace(/```(?:markdown)?/gi,'').split(/\\r?\\n/)){{const line=raw.trim();if(!line){{list=null;continue;}}let node;if(/^#{{1,3}}\\s/.test(line)){{node=document.createElement('h3');node.textContent=line.replace(/^#{{1,3}}\\s+/,'' );list=null;}}else if(/^[-*]\\s+/.test(line)){{if(!list){{list=document.createElement('ul');target.appendChild(list);}}node=document.createElement('li');node.textContent=line.replace(/^[-*]\\s+/,'' );list.appendChild(node);continue;}}else if(/^\\d+[.)]\\s+/.test(line)){{if(!list||list.tagName!=='OL'){{list=document.createElement('ol');target.appendChild(list);}}node=document.createElement('li');node.textContent=line.replace(/^\\d+[.)]\\s+/,'' );list.appendChild(node);continue;}}else{{node=document.createElement('p');node.textContent=line.replace(/\\*\\*/g,'');list=null;}}target.appendChild(node);}}}};
      form.addEventListener('submit',async(event)=>{{event.preventDefault();const button=form.querySelector('.run-button');const status=document.getElementById('ai-status');const result=document.getElementById('ai-result');button.disabled=true;status.className='status running';status.textContent='Analyzing your inputs…';result.hidden=true;const values={{}};for(const field of form.elements){{if(!field.name)continue;values[field.name]=field.dataset.type==='number'?Number(field.value||0):field.value;}}try{{const response=await fetch('/api/ai/run',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{aiPageId:Number(form.dataset.pageId),values}})}});const payload=await response.json();if(!response.ok)throw new Error(payload.error||'AI request failed');renderProfessional(payload.result.content,document.getElementById('ai-result-body'));document.getElementById('ai-model').textContent=payload.result.model||payload.result.provider||'OpenRouter';result.hidden=false;status.className='status complete';status.textContent='Analysis completed';result.scrollIntoView({{behavior:'smooth',block:'start'}});}}catch(error){{status.className='status error';status.textContent=error.message;}}finally{{button.disabled=false;}}}});
    }})();
    </script>'''

def route_candidate(row):
    if row.get("ai_route"): return row["ai_route"]
    routes=json.loads(row.get("routes_json") or "[]")
    candidates=[route for route in routes if route!="/" and route.startswith("/") and not route.startswith("/api/") and ":" not in route and "Page" not in route]
    return sorted(candidates,key=lambda route:(route.count("/"),len(route)))[0] if candidates else ""

def preferred_route(row, route_counts):
    candidate=route_candidate(row)
    if candidate and (row.get("ai_route") or route_counts.get(candidate,0)==1): return candidate
    return "/?feature="+str(row["id"])

def navigation_links(items, selected, route_counts):
    return "".join(f'<a class="nav-item {"active" if selected and row["id"]==selected["id"] else ""}" href="{html.escape(preferred_route(row,route_counts))}"><span>{html.escape(row["name"])}</span><small>{"AI" if row["ai_route"] else row["evidence_count"]}</small></a>' for row in items)

def source_overview_html(source_row,feature_rows,route_counts):
    feature_links="".join(f'<li><a href="{html.escape(preferred_route(feature,route_counts))}">{html.escape(feature["name"])}</a> <small>{"AI feature" if feature["ai_route"] else html.escape(feature["tier"]+" feature")}</small></li>' for feature in feature_rows)
    if not feature_links: feature_links='<li>No user-facing features are assigned to this source app.</li>'
    table_stats=rows("SELECT COUNT(*) AS table_count,COALESCE(SUM(row_count),0) AS row_count FROM seed_table_registry WHERE source_project=?",(source_row["project"],))[0]
    return f'''<section class="detail source-overview"><div class="eyebrow">Source application</div><h1>{html.escape(source_row["project"])}</h1><p class="lead">Source overview for this merged specialization. Select a feature below to open its workflow.</p><dl><dt>Available features</dt><dd>{len(feature_rows)}</dd><dt>Native tables</dt><dd>{table_stats["table_count"]}</dd><dt>Seeded records</dt><dd>{table_stats["row_count"]}</dd><dt>Source location</dt><dd><code>{html.escape(source_row["source_path"])}</code></dd></dl><h2>Features from this source</h2><ul class="source-feature-list">{feature_links}</ul></section>'''

STRICT_FEATURE_VISIBILITY = "(ai_pages.id IS NOT NULL OR EXISTS (SELECT 1 FROM feature_table_links visible_link JOIN seed_table_registry visible_table ON visible_table.id=visible_link.table_id WHERE visible_link.feature_id=features.id AND visible_table.row_count>0))"

def feature_visibility_rule():
    strict_count=rows("""SELECT COUNT(DISTINCT features.id) AS count FROM features
        LEFT JOIN ai_pages ON ai_pages.feature_id=features.id WHERE """+STRICT_FEATURE_VISIBILITY)[0]["count"]
    if strict_count>=2: return STRICT_FEATURE_VISIBILITY,False
    if strict_count==1:
        supplements=rows("""SELECT features.id FROM features JOIN feature_navigation ON feature_navigation.feature_id=features.id
            LEFT JOIN ai_pages ON ai_pages.feature_id=features.id
            WHERE feature_navigation.tier IN ('primary','secondary') AND NOT """+STRICT_FEATURE_VISIBILITY+" ORDER BY feature_navigation.priority DESC,features.name LIMIT 1")
        if supplements: return f"({STRICT_FEATURE_VISIBILITY} OR features.id={int(supplements[0]['id'])})",False
    user_facing=rows("SELECT COUNT(*) AS count FROM feature_navigation WHERE tier IN ('primary','secondary')")[0]["count"]
    if user_facing: return "feature_navigation.tier IN ('primary','secondary')",True
    return "1=1",True

def render(query, request_path="/"):
    search=query.get("q",[""])[0].strip(); source=query.get("source",[""])[0].strip(); feature_id=query.get("feature",[""])[0]; table_name=query.get("table",[""])[0]
    visibility_rule,fallback_mode=feature_visibility_rule(); where=[visibility_rule]; params=[]
    if search: where.append("(name LIKE ? OR aliases_json LIKE ?)"); params += [f"%{search}%",f"%{search}%"]
    if source: where.append("source_projects_json LIKE ?"); params.append(f'%"{source}"%')
    clause=" WHERE "+" AND ".join(where) if where else ""
    feature_rows=rows("SELECT features.*,ai_pages.route AS ai_route,feature_navigation.tier,feature_navigation.priority FROM features LEFT JOIN ai_pages ON ai_pages.feature_id=features.id JOIN feature_navigation ON feature_navigation.feature_id=features.id"+clause+" ORDER BY CASE feature_navigation.tier WHEN 'primary' THEN 1 WHEN 'secondary' THEN 2 ELSE 3 END,feature_navigation.priority DESC,features.name",params)
    if fallback_mode and feature_rows and not any(row["tier"] in {"primary","secondary"} for row in feature_rows):
        for index,row in enumerate(feature_rows): row["tier"]="primary" if index<24 else "secondary"
    route_counts={}
    for row in feature_rows:
        candidate=route_candidate(row)
        if candidate: route_counts[candidate]=route_counts.get(candidate,0)+1
    selected=None
    if request_path!="/":
        route_page=rows("SELECT feature_id FROM ai_pages WHERE route=?",(request_path,));
        if route_page and route_page[0]["feature_id"]: feature_id=str(route_page[0]["feature_id"])
        elif not route_page:
            route_feature=next((row for row in feature_rows if request_path in json.loads(row.get("routes_json") or "[]")),None)
            if route_feature: feature_id=str(route_feature["id"])
    if feature_id.isdigit():
        found=rows("SELECT features.*,ai_pages.route AS ai_route,feature_navigation.tier,feature_navigation.priority FROM features LEFT JOIN ai_pages ON ai_pages.feature_id=features.id JOIN feature_navigation ON feature_navigation.feature_id=features.id WHERE features.id=? AND "+visibility_rule,(int(feature_id),)); selected=found[0] if found else None
        if selected and fallback_mode and selected["tier"]=="technical": selected["tier"]="primary"
    if selected is None and feature_rows and not table_name and not source and request_path=="/": selected=feature_rows[0]
    sources=rows("SELECT * FROM source_projects ORDER BY project")
    if search:
        primary_rows=feature_rows; secondary_rows=[]; technical_rows=[]
    else:
        primary_rows=[row for row in feature_rows if row["tier"]=="primary"]
        secondary_rows=[row for row in feature_rows if row["tier"]=="secondary"]
        technical_rows=[row for row in feature_rows if row["tier"]=="technical"]
    primary_links=navigation_links(primary_rows,selected,route_counts) or '<p class="empty">No features match.</p>'
    secondary_links=navigation_links(secondary_rows,selected,route_counts)
    technical_links=navigation_links(technical_rows,selected,route_counts)
    workflow_link='<a href="/workflows" style="display:flex;justify-content:space-between;gap:20px;background:#d7f0f4;color:#083344;text-decoration:none;padding:15px 18px;border:1px solid #9ecbd1;border-radius:10px;margin-bottom:24px"><strong>Open operations workspace</strong><span>Create, manage, and advance records →</span></a>' if WORKFLOW_ENGINE.workflows else ""
    source_links="".join(f'<a class="source-item {"active" if source==row["project"] else ""}" href="/?source={quote(row["project"])}">{html.escape(row["project"])} <small>{row["seed_row_count"]} rows</small></a>' for row in sources)
    detail='<section class="empty-main"><h2>No feature selected</h2></section>'
    table_payload=native_table_payload(table_name) if table_name else None
    source_row=next((row for row in sources if row["project"]==source),None)
    if source_row and not table_payload and not selected:
        detail=source_overview_html(source_row,feature_rows,route_counts)
    elif table_payload:
        detail=native_table_html(table_payload)
    elif selected:
        ai_pages=[page for page in rows("SELECT * FROM ai_pages WHERE feature_id=? ORDER BY id",(selected["id"],)) if ai_binding_is_valid(page)][:1]
        if ai_pages: detail=ai_page_html(ai_pages[0])
        else:
         feature=feature_payload(selected); evidence=rows("SELECT * FROM feature_evidence WHERE feature_id=? ORDER BY kind,source_project,evidence_path",(feature["id"],))
         aliases=", ".join(feature["aliases"]) or "None"; routes="".join(f"<li><code>{html.escape(route)}</code></li>" for route in feature["routes"]) or "<li>None</li>"
         evidence_rows="".join(f'<tr><td>{html.escape(item["kind"])}</td><td>{html.escape(item["source_project"])}</td><td><code>{html.escape(item["evidence_path"])}</code></td><td><code>{html.escape(item["route"])}</code></td></tr>' for item in evidence)
         related=rows("SELECT seed_table_registry.*,feature_table_links.relevance_score FROM feature_table_links JOIN seed_table_registry ON seed_table_registry.id=feature_table_links.table_id WHERE feature_table_links.feature_id=? ORDER BY feature_table_links.relevance_score DESC,seed_table_registry.physical_table",(feature["id"],))
         related_links="".join(f'<li><a href="/?table={quote(table["physical_table"])}">{html.escape(table["physical_table"])}</a> — {table["row_count"]} rows</li>' for table in related) or '<li>No existing native seed tables for these sources.</li>'
         if related and related[0]["relevance_score"]>=40:
          detail=feature_table_html(feature,native_table_payload(related[0]["physical_table"]),related)
         else:
          detail=f'''<section class="detail"><div class="eyebrow">{" · ".join(feature["kinds"])}</div><h1>{html.escape(feature["name"])}</h1><p class="lead">Canonical combined feature represented once across {len(feature["consolidatedFromApps"])} merged app(s).</p><dl><dt>Existing aliases</dt><dd>{html.escape(aliases)}</dd><dt>Source projects</dt><dd>{html.escape(", ".join(feature["sourceProjects"]))}</dd></dl><h2>Connected data</h2><ul>{related_links}</ul><h2>Routes</h2><ul>{routes}</ul><h2>Source evidence</h2><div class="table"><table><thead><tr><th>Kind</th><th>Project</th><th>Path</th><th>Route</th></tr></thead><tbody>{evidence_rows}</tbody></table></div></section>'''
    detail=workflow_link+detail
    return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(MANIFEST["title"])}</title><style>
:root{{--ink:#17202a;--muted:#64748b;--line:#d9e2ec;--brand:#0b6073;--side:#102a43;--active:#d7f0f4;--success:#13795b;--danger:#b42318}}*{{box-sizing:border-box}}body{{margin:0;font:14px system-ui;color:var(--ink);background:#f7f9fb}}.shell{{display:grid;grid-template-columns:340px minmax(0,1fr);min-height:100vh}}aside{{position:sticky;top:0;height:100vh;overflow:auto;background:var(--side);color:white;padding:20px 16px}}aside h1{{font-size:20px;margin:0 0 5px}}.sub{{color:#b8c8d8;margin:0 0 18px}}.search{{width:100%;height:40px;border:0;border-radius:6px;padding:0 10px;margin-bottom:14px}}.section-title{{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:#9fb3c8;margin:18px 8px 8px}}.nav-item,.source-item{{display:flex;justify-content:space-between;gap:8px;color:#e7eef5;text-decoration:none;padding:8px;border-radius:6px;line-height:1.25}}.nav-item.active,.source-item.active{{background:var(--active);color:#083344;font-weight:800}}small{{color:#9fb3c8}}.active small{{color:#0b6073}}.admin-explorer{{margin-top:20px;border-top:1px solid #334e68;padding-top:12px}}.admin-explorer>summary,.more-features>summary,.feature-registry>summary{{cursor:pointer;color:#b8c8d8;font-size:11px;font-weight:900;text-transform:uppercase;letter-spacing:.09em;padding:8px;list-style-position:inside}}.more-features{{margin-top:10px}}.feature-registry{{margin:8px 0}}.admin-body{{padding-bottom:8px}}main{{padding:34px;min-width:0}}.detail{{max-width:100%}}h1{{font-size:34px;margin:5px 0 8px}}h2{{margin-top:30px}}.eyebrow{{color:var(--brand);font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.08em}}.lead{{color:var(--muted);font-size:16px}}dl{{display:grid;grid-template-columns:150px 1fr;gap:8px 14px;background:white;border:1px solid var(--line);padding:16px}}dt{{font-weight:800}}dd{{margin:0}}.table{{overflow:auto;border:1px solid var(--line);background:white}}table{{border-collapse:collapse;width:100%;min-width:760px}}th,td{{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid var(--line)}}th{{background:#edf2f7;font-size:11px;text-transform:uppercase;white-space:nowrap}}.record-row{{cursor:pointer;transition:background .15s}}.record-row:hover,.record-row:focus{{background:#e8f5f7;outline:none}}code{{word-break:break-word}}.empty{{padding:8px;color:#b8c8d8}}.preset-bar{{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:20px 0 0}}.preset-bar span{{font-size:12px;font-weight:900;color:var(--muted);text-transform:uppercase}}.preset-button{{border:1px solid #9fb3c8;background:white;color:#0b6073;padding:8px 11px;border-radius:999px;font-weight:800;cursor:pointer}}.preset-button:hover{{background:#e8f5f7}}.form-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;background:white;border:1px solid var(--line);padding:22px;margin-top:12px;border-radius:10px}}.form-group{{display:grid;gap:7px}}.form-group.full{{grid-column:1/-1}}.form-group label{{font-weight:800}}.form-group input,.form-group select,.form-group textarea{{width:100%;border:1px solid #bcccdc;border-radius:7px;padding:11px;font:inherit}}.form-group textarea{{min-height:120px}}.actions{{margin-top:16px}}.run-button{{border:0;border-radius:7px;background:var(--brand);color:white;padding:12px 20px;font-weight:900;cursor:pointer}}.run-button:disabled{{opacity:.6}}.status{{margin-top:14px;font-weight:800}}.status.running{{color:#8a5b00}}.status.complete{{color:var(--success)}}.status.error{{color:var(--danger)}}.ai-result{{margin-top:22px;background:white;border:1px solid var(--line);border-radius:12px;box-shadow:0 12px 35px rgba(16,42,67,.09);overflow:hidden}}.result-heading{{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:20px 24px;border-bottom:1px solid var(--line);background:linear-gradient(135deg,#eefbfc,#fff)}}.result-heading h2{{margin:3px 0 0}}.model-chip{{background:#102a43;color:white;padding:6px 10px;border-radius:999px;font-size:11px;font-weight:800}}.result-body{{padding:24px;font-size:15px;line-height:1.65}}.result-body h3{{margin:24px 0 8px;color:#0b6073}}.result-body h3:first-child{{margin-top:0}}.result-body p{{margin:0 0 12px}}.result-body li{{margin:6px 0}}.binding{{margin-top:24px;color:var(--muted)}}.record-modal{{border:0;padding:0;background:transparent;width:min(720px,calc(100% - 32px));max-width:none}}.record-modal::backdrop{{background:rgba(8,25,40,.68);backdrop-filter:blur(3px)}}.modal-panel{{background:white;border-radius:14px;box-shadow:0 24px 80px rgba(0,0,0,.3);max-height:82vh;overflow:auto}}.modal-head{{position:sticky;top:0;display:flex;justify-content:space-between;align-items:center;padding:20px 24px;background:white;border-bottom:1px solid var(--line)}}.modal-head h2{{margin:0}}.modal-close{{border:0;background:#edf2f7;width:36px;height:36px;border-radius:50%;font-size:22px;cursor:pointer}}.modal-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0;padding:12px 24px 24px}}.modal-field{{padding:14px 0;border-bottom:1px solid var(--line)}}.modal-field:nth-child(odd){{padding-right:18px}}.modal-label{{display:block;color:var(--muted);font-size:11px;font-weight:900;text-transform:uppercase;margin-bottom:5px}}.modal-value{{white-space:pre-wrap;word-break:break-word}}@media(max-width:800px){{.shell{{grid-template-columns:1fr}}aside{{position:relative;height:52vh}}main{{padding:20px}}.form-grid,.modal-grid{{grid-template-columns:1fr}}.modal-field:nth-child(odd){{padding-right:0}}}}
</style></head><body><div class="shell"><aside><h1>{html.escape(MANIFEST["title"])}</h1><p class="sub">{html.escape(MANIFEST["industry"])} · {len(primary_rows)} primary workflows</p><form><input class="search" name="q" value="{html.escape(search)}" placeholder="Search all features"></form><div class="section-title">{"Search results" if search else "Primary features"}</div>{primary_links}{f'<details class="more-features" {"open" if selected and selected.get("tier")=="secondary" else ""}><summary>More Features ({len(secondary_rows)})</summary>{secondary_links}</details>' if secondary_rows else ''}<details class="admin-explorer" {"open" if source or (selected and selected.get("tier")=="technical") else ""}><summary>Administration</summary><div class="admin-body"><div class="section-title">Source apps</div><a class="source-item" href="/">All source apps</a>{source_links}<details class="feature-registry" {"open" if selected and selected.get("tier")=="technical" else ""}><summary>Feature Registry ({len(technical_rows)})</summary>{technical_links or '<p class="empty">No technical features.</p>'}</details></div></details></aside><main>{detail}</main></div><dialog id="record-modal" class="record-modal"><div class="modal-panel"><header class="modal-head"><div><div class="eyebrow">Record details</div><h2 id="modal-title">Record</h2></div><button class="modal-close" type="button" aria-label="Close">×</button></header><div id="modal-grid" class="modal-grid"></div></div></dialog><script>
(()=>{{const activeSource=document.querySelector('.source-item.active');if(activeSource)requestAnimationFrame(()=>{{const sidebar=activeSource.closest('aside');if(sidebar)sidebar.scrollTop=Math.max(0,activeSource.offsetTop-sidebar.clientHeight/2);}});const modal=document.getElementById('record-modal'),grid=document.getElementById('modal-grid'),title=document.getElementById('modal-title');if(!modal)return;const humanize=key=>String(key).replace(/_/g,' ').replace(/\\b\\w/g,char=>char.toUpperCase());const openRecord=element=>{{const record=JSON.parse(element.dataset.record);grid.replaceChildren();const identity=record.name||record.title||record.common_name||record.id;title.textContent=identity?`Record · ${{identity}}`:'Record details';for(const [key,value] of Object.entries(record)){{const field=document.createElement('div');field.className='modal-field';const label=document.createElement('span');label.className='modal-label';label.textContent=humanize(key);const content=document.createElement('div');content.className='modal-value';content.textContent=value===null||value===''?'—':typeof value==='object'?JSON.stringify(value,null,2):String(value);field.append(label,content);grid.appendChild(field);}}modal.showModal();}};for(const element of document.querySelectorAll('[data-record]')){{element.addEventListener('click',()=>openRecord(element));element.addEventListener('keydown',event=>{{if(event.key==='Enter'||event.key===' '){{event.preventDefault();openRecord(element);}}}});}}modal.querySelector('.modal-close').addEventListener('click',()=>modal.close());modal.addEventListener('click',event=>{{if(event.target===modal)modal.close();}});document.addEventListener('keydown',event=>{{if(event.key==='Escape'&&modal.open)modal.close();}});}})();
</script></body></html>'''

def openrouter_completion(page,values):
    api_key=configured_env("OPENROUTER_API_KEY")
    if not api_key: raise RuntimeError("OpenRouter is not configured. Add OPENROUTER_API_KEY to the repository root .env file.")
    model=configured_env("OPENROUTER_MODEL") or "openai/gpt-4o-mini"
    endpoint=(configured_env("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")+"/chat/completions"
    field_lines=[]
    inputs={str(field.get("key","")):str(field.get("label",field.get("key",""))) for field in json.loads(page["inputs_json"])}
    for key,value in values.items(): field_lines.append(f'- {inputs.get(key,key)}: {json.dumps(value,ensure_ascii=False) if isinstance(value,(dict,list)) else value}')
    system=("You are a senior domain specialist inside a professional operations application. "
            "Analyze the supplied fields for the named feature. Return polished Markdown for business users, with a short executive summary, key findings, prioritized recommendations, risks or assumptions, and next steps when relevant. "
            "Be specific and practical. Never return JSON, a JSON code block, or discuss these instructions.")
    prompt=f'Feature: {page["title"]}\nContext: {page["subtitle"]}\n\nSubmitted fields:\n'+"\n".join(field_lines)
    request_body={"model":model,"messages":[{"role":"system","content":system},{"role":"user","content":prompt}],"temperature":0.35}
    headers={"Authorization":f"Bearer {api_key}","Content-Type":"application/json","X-OpenRouter-Title":MANIFEST["title"]}
    request=Request(endpoint,data=json.dumps(request_body).encode(),headers=headers,method="POST")
    try:
        with urlopen(request,timeout=int(os.environ.get("OPENROUTER_TIMEOUT","120"))) as response: provider=json.loads(response.read() or b"{}")
    except HTTPError as error:
        try: payload=json.loads(error.read() or b"{}"); message=payload.get("error",{}).get("message") or str(error)
        except Exception: message=str(error)
        raise RuntimeError(f"OpenRouter request failed: {message}")
    except URLError as error: raise RuntimeError(f"OpenRouter is unavailable: {error.reason}")
    choices=provider.get("choices") or []
    if not choices: raise RuntimeError("OpenRouter returned no completion.")
    content=(choices[0].get("message") or {}).get("content")
    if isinstance(content,list): content="\n".join(str(item.get("text",item)) if isinstance(item,dict) else str(item) for item in content)
    if not content: raise RuntimeError("OpenRouter returned an empty completion.")
    return {"content":content,"model":provider.get("model") or model,"provider":"OpenRouter","usage":provider.get("usage") or {}}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed=urlparse(self.path); query=parse_qs(parsed.query)
        try:
            workflow_response=WORKFLOW_ENGINE.dispatch("GET",parsed.path,query,headers=dict(self.headers))
            if workflow_response: return self.send_workflow_response(workflow_response)
        except WorkflowError as error: return self.send_json(error.payload(),error.status)
        if parsed.path=="/api/manifest": return self.send_json(MANIFEST)
        if parsed.path=="/api/features":
            visibility_rule,_=feature_visibility_rule()
            return self.send_json([feature_payload(row) for row in rows("SELECT features.* FROM features LEFT JOIN ai_pages ON ai_pages.feature_id=features.id JOIN feature_navigation ON feature_navigation.feature_id=features.id WHERE "+visibility_rule+" ORDER BY features.name")])
        if parsed.path=="/api/tables": return self.send_json(rows("SELECT * FROM seed_table_registry ORDER BY physical_table"))
        if parsed.path in {"/api/table","/api/seed-data"}:
            name=query.get("name",[""])[0]
            if name: return self.send_json(native_table_payload(name) or {"error":"table not found"})
            return self.send_json([native_table_payload(row["physical_table"]) for row in rows("SELECT physical_table FROM seed_table_registry ORDER BY physical_table")])
        workflow_ui=re.fullmatch(r"/workflows(?:/([^/]+))?/?",parsed.path)
        if workflow_ui:
            body=render_workflow_ui(MANIFEST["title"],workflow_ui.group(1) or "").encode()
            self.send_response(200); self.send_header("content-type","text/html; charset=utf-8"); self.send_header("content-length",str(len(body))); self.end_headers(); self.wfile.write(body); return
        body=render(query,parsed.path).encode(); self.send_response(200); self.send_header("content-type","text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        parsed=urlparse(self.path)
        if parsed.path.startswith("/api/workflows/") or parsed.path.startswith("/api/product/"):
            return self.handle_workflow_mutation("POST",parsed)
        if parsed.path!="/api/ai/run": return self.send_json({"error":"not found"},404)
        try:
            length=int(self.headers.get("content-length","0")); payload=json.loads(self.rfile.read(length) or b"{}")
            page_rows=rows("SELECT * FROM ai_pages WHERE id=?",(int(payload.get("aiPageId",0)),))
            if not page_rows: return self.send_json({"error":"AI page not found"},404)
            page=page_rows[0]
            if not ai_binding_is_valid(page): return self.send_json({"error":"AI page binding is invalid"},409)
            values=payload.get("values",{})
            try:
                result=openrouter_completion(page,values)
                execute("INSERT INTO ai_runs(ai_page_id,input_json,output_json,error,created_at) VALUES (?,?,?,?,?)",(page["id"],json.dumps(values),json.dumps(result),None,datetime.now(timezone.utc).isoformat()))
                return self.send_json({"result":result})
            except Exception as error:
                message=str(error); execute("INSERT INTO ai_runs(ai_page_id,input_json,error,created_at) VALUES (?,?,?,?)",(page["id"],json.dumps(values),message,datetime.now(timezone.utc).isoformat())); return self.send_json({"error":message},503)
        except Exception as error: return self.send_json({"error":str(error)},500)
    def do_PATCH(self):
        return self.handle_workflow_mutation("PATCH",urlparse(self.path))
    def do_DELETE(self):
        return self.handle_workflow_mutation("DELETE",urlparse(self.path),read_body=False)
    def handle_workflow_mutation(self,method,parsed,read_body=True):
        try:
            payload={}
            if read_body:
                length=int(self.headers.get("content-length","0"))
                if length>2_000_000: raise WorkflowError("Request body is too large.",413,"payload_too_large")
                try: payload=json.loads(self.rfile.read(length) or b"{}")
                except json.JSONDecodeError: raise WorkflowError("Request body must be valid JSON.",400,"invalid_json")
            response=WORKFLOW_ENGINE.dispatch(method,parsed.path,parse_qs(parsed.query),payload,dict(self.headers))
            if response: return self.send_workflow_response(response)
            return self.send_json({"error":"not found","code":"not_found"},404)
        except WorkflowError as error: return self.send_json(error.payload(),error.status)
        except Exception as error: return self.send_json({"error":str(error),"code":"internal_error"},500)
    def send_workflow_response(self,response):
        headers=response.headers or {}
        if response.body is None: body=b""
        elif isinstance(response.body,bytes): body=response.body
        elif isinstance(response.body,str): body=response.body.encode()
        else: body=json.dumps(response.body,indent=2,default=str).encode()
        self.send_response(response.status); self.send_header("content-type",response.content_type); self.send_header("content-length",str(len(body)))
        for key,value in headers.items(): self.send_header(key,value)
        self.end_headers()
        if body: self.wfile.write(body)
    def send_json(self,payload,status=200):
        body=json.dumps(payload,indent=2,default=str).encode(); self.send_response(status); self.send_header("content-type","application/json"); self.send_header("content-length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,format,*args): pass

def main():
    display_host = "127.0.0.1" if HOST == "0.0.0.0" else HOST
    print(f'{MANIFEST["title"]}: http://{display_host}:{PORT}')
    try: ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
    except KeyboardInterrupt: pass

if __name__ == "__main__": main()
