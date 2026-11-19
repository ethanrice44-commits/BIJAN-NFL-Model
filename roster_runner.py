#!/usr/bin/env python3
"""BIJAN V0.3.5.4 automatic roster evidence capture (RESEARCH/NOT BET-READY).

Fetches future schedule, nflverse candidate rosters/depth charts, and (with licensed
Sportradar API key) game-roster statuses. Unknown or unpaired identities BLOCK.
Captures immutable pre-kickoff evidence; does not create calibrated projections.
"""
from __future__ import annotations
import argparse, datetime as dt, hashlib, io, json, os, re, sys
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
import requests

UTC=dt.timezone.utc
NFLVERSE='https://github.com/nflverse/nflverse-data/releases/download'
CAND_POS={'QB','RB','HB','FB','WR','TE'}
RB_POS={'RB','HB','FB'}
OUT_STATUSES={'deactivated'}
PROVIDER_STATUS={'started','played','dnp','deactivated'}


def utcnow(): return dt.datetime.now(UTC)
def iso(t): return t.astimezone(UTC).isoformat().replace('+00:00','Z')
def sha(data): return hashlib.sha256(data).hexdigest()
def norm(s): return re.sub(r'[^a-z0-9]+','',str(s).casefold())
def fetch_bytes(url, headers=None):
    r=requests.get(url,headers=headers or {'User-Agent':'BIJAN research roster collector/0.3.5.4'},timeout=60)
    r.raise_for_status(); return r.content

def source_rows(df,team):
    return df[df['team'].astype(str).str.upper()==str(team).upper()].copy()

def nflverse_candidates(roster,depth,team,now,kickoff):
    needed={'team','gsis_id','position','full_name','status'}
    dn={'dt','team','gsis_id','pos_abb','pos_rank'}
    if needed-set(roster.columns): raise ValueError(f'nflverse roster schema missing {needed-set(roster.columns)}')
    if dn-set(depth.columns): raise ValueError(f'nflverse depth schema missing {dn-set(depth.columns)}')
    ros=source_rows(roster,team)
    ros=ros[ros.position.astype(str).str.upper().isin(CAND_POS)]
    d=source_rows(depth,team)
    d['date']=pd.to_datetime(d.dt,utc=True,errors='coerce')
    cutoff=pd.Timestamp(min(now,kickoff))
    d=d[(d.date<=cutoff)&d.pos_abb.astype(str).str.upper().isin(CAND_POS)].sort_values('date',ascending=False)
    d=d.drop_duplicates('gsis_id')
    rank={str(row.gsis_id):(str(row.pos_rank),str(row.dt)) for row in d.itertuples() if pd.notna(row.gsis_id)}
    out=[]
    for row in ros.itertuples():
        pid=str(row.gsis_id)
        if not pid or pid.lower()=='nan': continue
        bucket=str(row.status).upper()
        # Reject unknown status values rather than treating them as eligible.
        status={'ACT':'ROSTER_CANDIDATE','ACTIVE':'ROSTER_CANDIDATE','PS':'PRACTICE_SQUAD',
                'IR':'RESERVE','RES':'RESERVE','INJURED RESERVE':'RESERVE'}.get(bucket,'UNVERIFIED_STATUS')
        rank_str,asof=rank.get(pid,('',''))
        out.append(dict(team=team,player_id=pid,player_name=str(row.full_name),
                        position='RB' if str(row.position).upper()=='HB' else str(row.position).upper(),
                        roster_status=bucket,roster_bucket=status,depth_rank=rank_str,
                        depth_asof_utc=asof,availability='UNKNOWN_NOT_VERIFIED',
                        evidence='nflverse roster and dated depth chart (candidate only)',
                        mapping_status='NOT_YET_MATCHED'))
    # Depth-only candidates must not vanish from the universe.
    present={r['player_id'] for r in out}
    for row in d.itertuples():
        pid=str(row.gsis_id)
        if pid and pid not in present:
            out.append(dict(team=team,player_id=pid,player_name=str(getattr(row,'player_name','')),
                            position='RB' if str(row.pos_abb).upper()=='HB' else str(row.pos_abb).upper(),
                            roster_status='UNKNOWN',roster_bucket='DEPTH_ONLY_UNVERIFIED',
                            depth_rank=str(row.pos_rank),depth_asof_utc=str(row.dt),
                            availability='UNKNOWN_NOT_VERIFIED',evidence='depth-only candidate',
                            mapping_status='NOT_YET_MATCHED'))
    return out

def sr_games(payload):
    return [g for week in payload.get('weeks',[]) for g in week.get('games',[])]

def parse_provider_game(game,expected_home,expected_away,kickoff):
    if set([game.get('home',{}).get('alias'),game.get('away',{}).get('alias')]) != {expected_home,expected_away}:
        raise ValueError('Provider game team mismatch; BLOCK')
    if game.get('status') in {'inprogress','complete','closed','halftime'}: raise ValueError('Provider game already started')
    scheduled=pd.to_datetime(game.get('scheduled'),utc=True,errors='coerce')
    if pd.isna(scheduled) or abs((scheduled.to_pydatetime()-kickoff).total_seconds())>15*60:
        raise ValueError('Provider kickoff mismatch; BLOCK')
    return game['id']

def merge_provider_status(candidates,sr_payload,now,kickoff):
    """Never silently assume 'not deactivated' means certified active."""
    for item in candidates: item['mapping_status']='NOT_YET_MATCHED'
    byname={}
    for i,r in enumerate(candidates): byname.setdefault((r['team'],norm(r['player_name'])),[]).append(i)
    team_stats={}
    if sr_payload:
        for side in ('home','away'):
            obj=sr_payload.get(side,{})
            team=obj.get('alias'); players=obj.get('players',[])
            if not team or not isinstance(players,list): raise ValueError('Unexpected Sportradar game roster schema')
            team_stats[team]={'n_provider_players':len(players),'n_deactivated':0,'unmatched':[],'ambiguous':[]}
            for p in players:
                status=str(p.get('status','')).lower()
                if status not in PROVIDER_STATUS:
                    # Before kickoff the feed may not be populated; never infer a declaration.
                    continue
                if status=='deactivated': team_stats[team]['n_deactivated']+=1
                key=(team,norm(p.get('name','')))
                ids=byname.get(key,[])
                if len(ids)==1:
                    rec=candidates[ids[0]]
                    rec['mapping_status']='UNIQUE_NAME_TEAM_MATCH_REQUIRES_ID_REVIEW'
                    rec['provider_status']=status
                    rec['provider_player_id']=str(p.get('id',''))
                    # Only a specific deactivated designation is treated as inactive.
                    if status=='deactivated': rec['availability']='CONFIRMED_PROVIDER_INACTIVE'
                    else: rec['availability']='PROVIDER_LISTED_NOT_INACTIVE'
                elif len(ids)>1: team_stats[team]['ambiguous'].append(p.get('name',''))
                elif p.get('position') in CAND_POS: team_stats[team]['unmatched'].append(p.get('name',''))
        # A feed can be partially preloaded until close to kickoff. This is evidence of
        # possible completeness, never a warranty that the feed is official/certified.
        for team,s in team_stats.items():
            s['potentially_complete']=(dt.timedelta(minutes=85)>=kickoff-now >= dt.timedelta(0)
                                      and s['n_provider_players']>=45 and s['n_deactivated']>=1
                                      and not s['ambiguous'] and not s['unmatched'])
    return team_stats

def report_game(game,candidates,stats,now,provider_used):
    teams=[game['away'],game['home']]; gate=[]
    for team in teams:
        player=[r for r in candidates if r['team']==team]
        qbs=[p for p in player if p['position']=='QB' and p['availability']!='CONFIRMED_PROVIDER_INACTIVE'
             and p['roster_bucket']=='ROSTER_CANDIDATE']
        rbs=[p for p in player if p['position'] in RB_POS and p['availability']!='CONFIRMED_PROVIDER_INACTIVE'
             and p['roster_bucket']=='ROSTER_CANDIDATE']
        issues=[]
        if len(qbs)<2: issues.append('Fewer than 2 provisional QB candidates')
        if not rbs: issues.append('No available RB candidate')
        if any(p['roster_bucket'] in {'UNVERIFIED_STATUS','DEPTH_ONLY_UNVERIFIED'} for p in player):
            issues.append('Unresolved roster bucket')
        s=stats.get(team,{})
        if not provider_used: issues.append('No game-day provider feed configured')
        elif not s.get('potentially_complete'):
            issues.append('Game-day provider feed not yet complete or unresolved')
        if not all(p['mapping_status']=='UNIQUE_NAME_TEAM_MATCH_REQUIRES_ID_REVIEW'
                   for p in player if p['position'] in RB_POS|{'QB'} and p['roster_bucket']=='ROSTER_CANDIDATE'):
            issues.append('Player ID crosswalk needs verification')
        issues+=['Starting QB/backup not independently confirmed',
                 'Practice-squad elevations/emergency QB not fully reconciled',
                 'Historical RB workload priors not certified']
        def rank_val(p):
            try: return int(float(p.get('depth_rank','')))
            except (ValueError,TypeError): return 999
        qbs=sorted(qbs,key=lambda x:(rank_val(x),x['player_name']))
        rbs=sorted(rbs,key=lambda x:(rank_val(x),x['player_name']))
        # Provisional ordering only; ties/missing ranks must be resolved by role inputs.
        qb1=qbs[0]['player_id'] if qbs and rank_val(qbs[0])<999 and (len(qbs)==1 or rank_val(qbs[0])!=rank_val(qbs[1])) else ''
        qb2=qbs[1]['player_id'] if len(qbs)>1 and rank_val(qbs[1])<999 and (len(qbs)==2 or rank_val(qbs[1])!=rank_val(qbs[2])) else ''
        if not qb1 or not qb2: issues.append('QB1/QB2 depth order ambiguous')
        gate.append(dict(game_id=game['id'],team=team,kickoff_utc=game['kickoff'],
                         provisional_qb1_id=qb1,provisional_qb2_id=qb2,
                         provisional_rb_depth_ids=';'.join(p['player_id'] for p in rbs),
                         provisional_qb_candidates=len(qbs),provisional_rb_candidates=len(rbs),
                         provider_players=s.get('n_provider_players',0),
                         provider_inactives=s.get('n_deactivated',0),
                         status='RESEARCH_ONLY_BLOCKED',market_ready=False,blocked_reasons='; '.join(issues)))
    return gate

def sample_games():
    # Purely fictional teams/players, never historical evidence.
    now=dt.datetime(2026,11,22,17,15,tzinfo=UTC)
    game={'id':'SYNTHETIC_GAME','home':'EXH','away':'EXA','kickoff':iso(now+dt.timedelta(minutes=75))}
    candidates=[]
    for team in ('EXH','EXA'):
        for i,pos in enumerate(['QB','QB','RB','RB','RB','RB']):
            candidates.append(dict(team=team,player_id=f'{team}{i}',player_name=f'{team} Player {i}',
                                   position=pos,roster_bucket='ROSTER_CANDIDATE',roster_status='ACT',
                                   depth_rank=str(i+1),depth_asof_utc=iso(now-dt.timedelta(days=1)),
                                   availability='UNKNOWN_NOT_VERIFIED',mapping_status='NOT_YET_MATCHED',
                                   evidence='SYNTHETIC'))
    sr={'home':{'alias':'EXH','players':[{'name':f'EXH Player {i}','position':p,
                         'status':'deactivated' if i==4 else 'dnp','id':f'h{i}'} for i,p in enumerate(['QB','QB','RB','RB','RB','RB'])]},
        'away':{'alias':'EXA','players':[{'name':f'EXA Player {i}','position':p,
                         'status':'deactivated' if i==5 else 'dnp','id':f'a{i}'} for i,p in enumerate(['QB','QB','RB','RB','RB','RB'])]}}
    return now,game,candidates,sr

def main():
    a=argparse.ArgumentParser();a.add_argument('--mode',choices=['demo','live'],default='demo');
    a.add_argument('--season',type=int,default=2026);a.add_argument('--output',default='output')
    a.add_argument('--lookahead-days',type=int,default=8); args=a.parse_args()
    now=utcnow();out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    if args.mode=='demo':
        now,game,players,provider=sample_games()
        stats=merge_provider_status(players,provider,now,pd.to_datetime(game['kickoff'],utc=True).to_pydatetime())
        gates=report_game(game,players,stats,now,True)
        assert any(p['availability']=='CONFIRMED_PROVIDER_INACTIVE' for p in players)
        assert all(g['market_ready']==False for g in gates)
        prefix='DEMO_SYNTHETIC'; games=[game]; sources={}
    else:
        # No historical backfill: capture only games that have not kicked off.
        key=os.environ.get('BIJAN_SPORTRADAR_KEY','');level=os.environ.get('BIJAN_SPORTRADAR_LEVEL','trial')
        if level not in {'trial','production'}: raise ValueError('Unsupported Sportradar access level')
        sr_base=f'https://api.sportradar.com/nfl/official/{level}/v7/en'
        schedule_url=f'{NFLVERSE}/schedules/games.csv'
        schedule=pd.read_csv(io.BytesIO(fetch_bytes(schedule_url)),dtype=str)
        req={'game_id','season','gameday','gametime','home_team','away_team'}
        if req-set(schedule): raise ValueError(f'Schedule schema changed: {req-set(schedule)}')
        schedule=schedule[schedule.season==str(args.season)]
        games=[]
        for row in schedule.to_dict('records'):
            if not row.get('gameday') or not row.get('gametime'): continue
            try: kickoff=dt.datetime.fromisoformat(f"{row['gameday']}T{row['gametime']}:00" if len(row['gametime'])==5 else f"{row['gameday']}T{row['gametime']}").replace(tzinfo=ZoneInfo('America/New_York')).astimezone(UTC)
            except ValueError: continue
            if now<kickoff<=now+dt.timedelta(days=args.lookahead_days):
                games.append({'id':row['game_id'],'home':row['home_team'],'away':row['away_team'],'kickoff':iso(kickoff)})
        if not games:
            print('No upcoming games; empty snapshot, no retroactive capture.');return
        # Don't fetch full season assets on every 15-minute poll when no game is near.
        nearest=min((pd.to_datetime(g['kickoff'],utc=True).to_pydatetime()-now for g in games))
        if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch" and nearest>dt.timedelta(hours=3) and (now.hour!=13 or now.minute>15)
            print('No games in next 3 hours; daily pregame refresh window not reached.');return
        roster_url=f'{NFLVERSE}/rosters/roster_{args.season}.parquet'
        depth_url=f'{NFLVERSE}/depth_charts/depth_charts_{args.season}.parquet'
        rbytes=fetch_bytes(roster_url);dbytes=fetch_bytes(depth_url)
        roster=pd.read_parquet(io.BytesIO(rbytes));depth=pd.read_parquet(io.BytesIO(dbytes))
        sources={'roster':{'url':roster_url,'sha256':sha(rbytes)},'depth':{'url':depth_url,'sha256':sha(dbytes)},
                 'schedule':{'url':schedule_url}}
        sr_schedule=None;sr_headers={'x-api-key':key} if key else None
        if key:
            sr_url=f'{sr_base}/games/{args.season}/REG/schedule.json'
            sr_bytes=fetch_bytes(sr_url,sr_headers);sr_schedule=json.loads(sr_bytes)
            sources['provider_schedule']={'url':sr_url,'sha256':sha(sr_bytes)}
        players=[];gates=[]
        for game in games:
            kickoff=pd.to_datetime(game['kickoff'],utc=True).to_pydatetime()
            p=nflverse_candidates(roster,depth,game['home'],now,kickoff)+nflverse_candidates(roster,depth,game['away'],now,kickoff)
            selected=None;stats={}
            if sr_schedule is not None:
                matches=[g for g in sr_games(sr_schedule) if
                         {g.get('home',{}).get('alias'),g.get('away',{}).get('alias')}=={game['home'],game['away']} and
                         abs((pd.to_datetime(g.get('scheduled'),utc=True).to_pydatetime()-kickoff).total_seconds())<=900]
                if len(matches)!=1: raise ValueError(f'Provider schedule match ambiguous for {game["id"]}: {len(matches)}')
                sr_id=parse_provider_game(matches[0],game['home'],game['away'],kickoff)
                game['sportradar_game_id']=sr_id
                if dt.timedelta(0)<kickoff-now<=dt.timedelta(minutes=90):
                    roster_api=f'{sr_base}/games/{sr_id}/roster.json'
                    raw=fetch_bytes(roster_api,sr_headers)
                    selected=json.loads(raw)
                    if selected.get('id')!=sr_id: raise ValueError('Provider game id mismatch')
                    if selected.get('status') in {'inprogress','halftime','closed','complete'}: raise ValueError('Feed not pregame')
                    sources[f'provider_{sr_id}']={'url':roster_api,'sha256':sha(raw),'captured_utc':iso(now)}
                    (out/f'{game["id"]}_provider_raw.json').write_bytes(raw)
            stats=merge_provider_status(p,selected,now,kickoff)
            gates+=report_game(game,p,stats,now,bool(selected));players+=p
        prefix='RESEARCH_ONLY'
    pd.DataFrame(games).to_csv(out/'game_schedule.csv',index=False)
    pd.DataFrame(players).to_csv(out/'candidate_pool.csv',index=False)
    pd.DataFrame(gates).to_csv(out/'simulation_gates.csv',index=False)
    # The integration output must be EMPTY while even one team in a matchup is blocked.
    # It must never leak unverified players into the betting-ready simulation.
    pd.DataFrame(columns=['game_id','team','player_id','eligibility_certified']).to_csv(
        out/'verified_players_for_engine.csv', index=False)
    manifest={'schema':'BIJAN_V0354','created_utc':iso(now),'mode':args.mode,
              'classification':prefix,'sources':sources,'all_market_ready':False}
    (out/'capture_manifest.json').write_text(json.dumps(manifest,indent=2))
    checksums={p.name:sha(p.read_bytes()) for p in out.iterdir() if p.is_file()}
    (out/'file_sha256.json').write_text(json.dumps(checksums,indent=2))
    print(f"Captured {len(games)} game(s), {len(players)} candidates; bet-ready=0 (blocked by design)")

if __name__=='__main__': main()
