package dev.flowpay.support;

import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/** PostgreSQL 是任务归属的权威；内存线程只能执行已经持有的数据库批次。 */
@Component
public class RunCoordinator {
    public record Claim(String runId,String sessionId,String executionId,long version,String input,
                        Identity.User owner,String agentTarget) {}
    private final JdbcTemplate db; private final TransactionTemplate tx; private final Json json;
    private final SingleInstanceLease instance; private final String agentTarget;
    private final int leaseSeconds,deadlineSeconds;
    public RunCoordinator(JdbcTemplate db,TransactionTemplate tx,Json json,SingleInstanceLease instance,
                          @Value("${support.agent-url}") String agentTarget,
                          @Value("${support.run-lease-seconds:30}") int leaseSeconds,
                          @Value("${support.run-deadline-seconds:180}") int deadlineSeconds) {
        if(leaseSeconds<15||deadlineSeconds<leaseSeconds) throw new IllegalArgumentException("Invalid run lease/deadline");
        this.db=db; this.tx=tx; this.json=json; this.instance=instance; this.agentTarget=agentTarget;
        this.leaseSeconds=leaseSeconds; this.deadlineSeconds=deadlineSeconds;
    }
    private void event(String sid,String run,String type,Object payload) {
        Long seq=db.queryForObject("UPDATE sessions SET next_seq=next_seq+1 WHERE id=? RETURNING next_seq",Long.class,sid);
        db.update("INSERT INTO session_events(session_id,seq,run_id,type,payload) VALUES (?,?,?,?,?::jsonb)",sid,seq,run,type,json.write(payload));
    }
    public Claim claimNext(Map<String,Object> deployment) {
        var candidates=db.queryForList("SELECT r.id,r.session_id FROM runs r WHERE r.status='queued' AND r.available_at<=now() ORDER BY r.created_at,r.id LIMIT 8");
        for(var candidate:candidates) {
            Claim result=tx.execute(status->{
                String rid=(String)candidate.get("id"),sid=(String)candidate.get("session_id");
                var scope=db.queryForList("SELECT s.tenant_id FROM sessions s JOIN runs r ON r.session_id=s.id WHERE r.id=?",rid);
                if(scope.isEmpty()) return null;
                db.queryForList("SELECT id FROM tenants WHERE id=? FOR UPDATE",scope.getFirst().get("tenant_id"));
                db.queryForList("SELECT id FROM sessions WHERE id=? FOR UPDATE",sid);
                var rows=db.queryForList("SELECT r.*,u.id uid,u.username,u.tenant_id,u.role,u.active user_active FROM runs r JOIN sessions s ON s.id=r.session_id JOIN app_users u ON u.id=s.owner_id WHERE r.id=? FOR UPDATE OF r",rid);
                if(rows.isEmpty()||!"queued".equals(rows.getFirst().get("status"))||!Boolean.TRUE.equals(rows.getFirst().get("user_active"))) return null;
                var slots=db.queryForList("SELECT slot_id FROM execution_slots WHERE execution_id IS NULL ORDER BY slot_id FOR UPDATE SKIP LOCKED LIMIT 1");
                if(slots.isEmpty()) return null;
                var row=rows.getFirst(); int slot=((Number)slots.getFirst().get("slot_id")).intValue();
                long version=((Number)row.get("claim_version")).longValue()+1;
                String execution="exec_"+UUID.randomUUID().toString().replace("-","");
                int changed=db.update("UPDATE runs SET status='running',attempt=attempt+1,claim_version=?,owner_instance=?,current_execution_id=?,started_at=COALESCE(started_at,now()),lease_until=now()+(?*interval '1 second'),deadline_at=now()+(?*interval '1 second'),execution_mode=?,execution_engine=? WHERE id=? AND status='queued'",
                    version,instance.instanceId(),execution,leaseSeconds,deadlineSeconds,deployment.get("mode"),deployment.get("engine"),rid);
                if(changed!=1) return null;
                db.update("INSERT INTO run_attempts(execution_id,run_id,claim_version,owner_instance,agent_target,state,lease_until) VALUES (?,?,?,?,?,'running',now()+(?*interval '1 second'))",
                    execution,rid,version,instance.instanceId(),agentTarget,leaseSeconds);
                db.update("UPDATE execution_slots SET execution_id=?,lease_until=now()+(?*interval '1 second') WHERE slot_id=?",execution,leaseSeconds,slot);
                db.update("UPDATE sessions SET status='running' WHERE id=?",sid);
                event(sid,rid,"run.started",Map.of("execution_id",execution,"claim_version",version));
                var owner=new Identity.User((String)row.get("uid"),(String)row.get("username"),(String)row.get("tenant_id"),(String)row.get("role"));
                return new Claim(rid,sid,execution,version,(String)row.get("input"),owner,agentTarget);
            });
            if(result!=null) return result;
        }
        return null;
    }
    public boolean heartbeat(Claim claim) {
        return Boolean.TRUE.equals(tx.execute(status->{
            int changed=db.update("UPDATE runs SET lease_until=now()+(?*interval '1 second') WHERE id=? AND status='running' AND current_execution_id=? AND claim_version=? AND owner_instance=? AND lease_until>now() AND deadline_at>now()",
                leaseSeconds,claim.runId(),claim.executionId(),claim.version(),instance.instanceId());
            if(changed!=1) return false;
            db.update("UPDATE run_attempts SET lease_until=now()+(?*interval '1 second') WHERE execution_id=? AND state='running'",leaseSeconds,claim.executionId());
            db.update("UPDATE execution_slots SET lease_until=now()+(?*interval '1 second') WHERE execution_id=?",leaseSeconds,claim.executionId());
            return true;
        }));
    }
    public boolean complete(Claim claim,String end,String answer,Object evidence,Object metrics,Object checkpoint,String mode,String engine) {
        return Boolean.TRUE.equals(tx.execute(status->{
            lockScope(claim.sessionId(),claim.runId());
            int changed=db.update("UPDATE runs SET status=?,answer=?,evidence=?::jsonb,metrics=?::jsonb,execution_mode=?,execution_engine=?,ended_at=now(),lease_until=null WHERE id=? AND status='running' AND current_execution_id=? AND claim_version=? AND owner_instance=? AND lease_until>now() AND deadline_at>=now()",
                end,answer,json.write(evidence),json.write(metrics),mode,engine,claim.runId(),claim.executionId(),claim.version(),instance.instanceId());
            if(changed!=1) return false;
            db.update("UPDATE sessions SET status=?,checkpoint=?::jsonb WHERE id=?",end,json.write(checkpoint),claim.sessionId());
            finishAttempt(claim.executionId(),"completed");
            event(claim.sessionId(),claim.runId(),"run.finished",Map.of("answer",answer,"status",end,"evidence",evidence));
            return true;
        }));
    }
    public boolean fail(Claim claim,String message) {
        return Boolean.TRUE.equals(tx.execute(status->{
            lockScope(claim.sessionId(),claim.runId());
            int changed=db.update("UPDATE runs SET status='failed',error=?,ended_at=now(),lease_until=null WHERE id=? AND status='running' AND current_execution_id=? AND claim_version=? AND owner_instance=? AND lease_until>now() AND deadline_at>=now()",message,claim.runId(),claim.executionId(),claim.version(),instance.instanceId());
            if(changed!=1) return false;
            db.update("UPDATE sessions SET status='failed' WHERE id=?",claim.sessionId());
            finishAttempt(claim.executionId(),"failed");
            event(claim.sessionId(),claim.runId(),"run.failed",Map.of("message",message));
            return true;
        }));
    }
    public void releaseRejected(Claim claim) {
        tx.executeWithoutResult(status->{
            lockScope(claim.sessionId(),claim.runId());
            int changed=db.update("UPDATE runs SET status='queued',owner_instance=null,current_execution_id=null,lease_until=null,deadline_at=null,available_at=now()+interval '1 second' WHERE id=? AND status='running' AND current_execution_id=? AND claim_version=? AND owner_instance=?",claim.runId(),claim.executionId(),claim.version(),instance.instanceId());
            if(changed==1) { finishAttempt(claim.executionId(),"expired"); db.update("UPDATE sessions SET status='queued' WHERE id=?",claim.sessionId()); }
        });
    }
    private void lockScope(String sid,String rid) {
        var tenant=db.queryForMap("SELECT tenant_id FROM sessions WHERE id=?",sid).get("tenant_id");
        db.queryForList("SELECT id FROM tenants WHERE id=? FOR UPDATE",tenant);
        db.queryForList("SELECT id FROM sessions WHERE id=? FOR UPDATE",sid);
        db.queryForList("SELECT id FROM runs WHERE id=? FOR UPDATE",rid);
    }
    private void finishAttempt(String execution,String state) {
        db.update("UPDATE run_attempts SET state=?,ended_at=now() WHERE execution_id=?",state,execution);
        db.update("UPDATE execution_slots SET execution_id=null,lease_until=null WHERE execution_id=?",execution);
    }
    private void expireAttempt(String execution) {
        db.update("UPDATE run_attempts SET state='expired',ended_at=now(),cancel_available_at=now(),cancel_deadline_at=now()+interval '2 minutes' WHERE execution_id=? AND state='running'",execution);
        db.update("UPDATE execution_slots SET execution_id=null,lease_until=null WHERE execution_id=?",execution);
    }
    public void cancelSession(String sid) {
        tx.executeWithoutResult(status->{
            var tenant=db.queryForMap("SELECT tenant_id FROM sessions WHERE id=?",sid).get("tenant_id");
            db.queryForList("SELECT id FROM tenants WHERE id=? FOR UPDATE",tenant);
            db.queryForList("SELECT id FROM sessions WHERE id=? FOR UPDATE",sid);
            var runs=db.queryForList("SELECT id,current_execution_id FROM runs WHERE session_id=? AND status IN ('queued','running') FOR UPDATE",sid);
            for(var row:runs) {
                String rid=(String)row.get("id"),execution=(String)row.get("current_execution_id");
                db.update("UPDATE runs SET status='canceled',ended_at=now(),lease_until=null WHERE id=?",rid);
                if(execution!=null) db.update("UPDATE run_attempts SET state='canceled',ended_at=now(),cancel_available_at=now(),cancel_deadline_at=now()+interval '2 minutes' WHERE execution_id=? AND state='running'",execution);
                if(execution!=null) db.update("UPDATE execution_slots SET lease_until=LEAST(lease_until,now()+interval '10 seconds') WHERE execution_id=?",execution);
                event(sid,rid,"run.canceled",Map.of());
            }
            if(!runs.isEmpty()) db.update("UPDATE sessions SET status='canceled' WHERE id=?",sid);
        });
    }
    public List<Map<String,Object>> pendingCancels() {
        return db.queryForList("SELECT execution_id,agent_target FROM run_attempts WHERE state IN ('canceled','expired') AND cancel_ack_at IS NULL AND cancel_available_at<=now() AND cancel_deadline_at>now() ORDER BY cancel_available_at LIMIT 8");
    }
    public void cancelFailed(String execution) {
        db.update("UPDATE run_attempts SET cancel_attempt=cancel_attempt+1,cancel_available_at=now()+(LEAST(cancel_attempt+1,30)*interval '1 second') WHERE execution_id=? AND state IN ('canceled','expired') AND cancel_ack_at IS NULL",execution);
    }
    public void acknowledgeCancel(String execution) {
        tx.executeWithoutResult(status->{
            db.update("UPDATE run_attempts SET cancel_ack_at=now() WHERE execution_id=? AND state IN ('canceled','expired')",execution);
            db.update("UPDATE execution_slots SET execution_id=null,lease_until=null WHERE execution_id=?",execution);
        });
    }
    public void recoverExpired() {
        db.update("UPDATE execution_slots s SET execution_id=null,lease_until=null WHERE s.lease_until<now() AND EXISTS (SELECT 1 FROM run_attempts a WHERE a.execution_id=s.execution_id AND a.state='canceled')");
        for(var item:db.queryForList("SELECT id,session_id FROM runs WHERE status='running' AND lease_until<now() ORDER BY lease_until LIMIT 8")) {
            tx.executeWithoutResult(status->{
                String rid=(String)item.get("id"),sid=(String)item.get("session_id"); lockScope(sid,rid);
                var rows=db.queryForList("SELECT attempt,current_execution_id FROM runs WHERE id=? AND status='running' AND lease_until<now()",rid);
                if(rows.isEmpty()) return;
                int attempt=((Number)rows.getFirst().get("attempt")).intValue(); String execution=(String)rows.getFirst().get("current_execution_id");
                if(execution!=null) expireAttempt(execution);
                if(attempt<2) {
                    db.update("UPDATE runs SET status='queued',owner_instance=null,current_execution_id=null,lease_until=null,deadline_at=null,available_at=now() WHERE id=?",rid);
                    db.update("UPDATE sessions SET status='queued' WHERE id=?",sid);
                    event(sid,rid,"run.recovered",Map.of("reason","lease_expired","attempt",attempt));
                } else {
                    db.update("UPDATE runs SET status='failed',error='恢复次数已达上限，请重新提交',ended_at=now(),lease_until=null WHERE id=?",rid);
                    db.update("UPDATE sessions SET status='failed' WHERE id=?",sid);
                    event(sid,rid,"run.failed",Map.of("message","恢复次数已达上限，请重新提交"));
                }
            });
        }
    }
    public Map<String,Object> status() {
        return Map.of("instance",instance.instanceId(),"mode",instance.mode(),
            "activeSlots",db.queryForObject("SELECT count(*) FROM execution_slots WHERE execution_id IS NOT NULL",Long.class),
            "queued",db.queryForObject("SELECT count(*) FROM runs WHERE status='queued'",Long.class),
            "oldestQueuedSeconds",db.queryForObject("SELECT COALESCE(extract(epoch FROM now()-min(created_at)),0) FROM runs WHERE status='queued'",Double.class),
            "pendingCancels",db.queryForObject("SELECT count(*) FROM run_attempts WHERE state IN ('canceled','expired') AND cancel_ack_at IS NULL AND cancel_deadline_at>now()",Long.class));
    }
}
