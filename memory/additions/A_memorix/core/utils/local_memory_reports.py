"""Local candidate reports and old derived-profile housekeeping."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import json,time,re
from .local_memory_policy import metadata,memory_kind,paragraph_active,visibility_key

def scheduled_reports(store,now=None):
 point=float(now if now is not None else time.time())
 directory=Path(store.data_dir).parent/'retention-reports';directory.mkdir(parents=True,exist_ok=True)
 state_file=directory/'schedule-state.json'
 try:state=json.loads(state_file.read_text(encoding='utf-8'))
 except FileNotFoundError:state={}
 result={'weekly_report':False,'old_snapshots_removed':0}
 stamp=datetime.fromtimestamp(point,timezone(timedelta(hours=8)))
 if point-float(state.get('weekly_at',0))>=7*86400:
  candidates=[];same={}
  for row in store.query('select * from paragraphs where is_deleted=0'):
   md=metadata(row.get('metadata'))
   if md.get('needs_review') or md.get('legacy_unreviewed'):
    if paragraph_active(row,now=point) or float(md.get('retention_archived_at',0))>=point-7*86400:candidates.append({'type':'review','paragraph_hash':row['hash'],'text':row['content'],'source':row.get('source'),'state':md.get('retention_state','active')})
   if paragraph_active(row,now=point) and memory_kind(row) in {'fact','shared_experience'}:
    key=(re.sub(r'\W+','',row['content']).casefold(),visibility_key(md,str(row.get('source',''))))
    if key in same:candidates.append({'type':'duplicate','paragraph_hash':row['hash'],'same_as':same[key],'text':row['content']})
    else:same[key]=row['hash']
  for claim in store.query("select claim_id,scope_id,value_text from fact_claims where status='conflicted'"):candidates.append({'type':'conflict',**claim})
  payload={'generated_at':stamp.isoformat(),'recent_days':7,'candidates':candidates,'count':len(candidates)}
  path=directory/(f'candidates-{stamp.isocalendar().year}-W{stamp.isocalendar().week:02d}.json')
  temp=path.with_suffix('.json.tmp');temp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)
  state['weekly_at']=point;state['weekly_file']=path.name;result['weekly_report']=True
 month=stamp.strftime('%Y-%m')
 if state.get('snapshot_month')!=month:
  with store.transaction(immediate=True) as connection:
   cursor=connection.execute("delete from person_profile_snapshots where updated_at<? and snapshot_id not in (select snapshot_id from (select snapshot_id,row_number() over(partition by person_id order by profile_version desc) rn from person_profile_snapshots) where rn=1) and lower(coalesce(source_note,'')) not like '%manual%'",(point-30*86400,))
   result['old_snapshots_removed']=cursor.rowcount
  state['snapshot_month']=month
 temp=state_file.with_suffix('.json.tmp');temp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(state_file)
 return result
