package dev.flowpay.support;

import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

@Service
public class TicketService {
    private final JdbcTemplate db; private final Identity identity; private final TransactionTemplate tx; private final Json json;
    public TicketService(JdbcTemplate db,Identity identity,TransactionTemplate tx,Json json) { this.db=db; this.identity=identity; this.tx=tx; this.json=json; }
    private Map<String,Object> ticket(Identity.User user,String id,boolean lock) {
        var rows=db.queryForList("SELECT * FROM tickets WHERE id=?"+(lock?" FOR UPDATE":""),id);
        if(rows.isEmpty()) throw new ApiError(404,"记录不存在或无权访问");
        identity.authorize(user,(String)rows.getFirst().get("tenant_id")); return rows.getFirst();
    }
    public List<Map<String,Object>> list(Identity.User user) {
        return db.queryForList("SELECT id,tenant_id,session_id,title,status,assignee_id,version,created_at FROM tickets t WHERE "
            +"(t.tenant_id=? AND ?='MERCHANT') OR EXISTS(SELECT 1 FROM support_grants g WHERE g.user_id=? AND g.tenant_id=t.tenant_id) "
            +"ORDER BY created_at DESC LIMIT 100",user.tenantId(),user.role(),user.id());
    }
    public Map<String,Object> detail(Identity.User user,String id) {
        var result=new java.util.LinkedHashMap<>(ticket(user,id,false));
        result.put("evidence",json.object("{\"value\":"+result.get("evidence")+"}").get("value"));
        result.put("history",db.queryForList("SELECT actor_id,from_status,to_status,note,created_at FROM ticket_history WHERE ticket_id=? ORDER BY id",id));
        return result;
    }
    public Map<String,Object> create(Identity.User user,String sid,String requestId,String title) {
        if(!user.role().equals("MERCHANT")) throw new ApiError(403,"仅商户可创建工单");
        return tx.execute(status->{
            // 商户行锁串行化本商户建单；唯一键仍是最后一道防线，幂等域为商户+请求编号。
            db.queryForList("SELECT id FROM tenants WHERE id=? FOR UPDATE",user.tenantId());
            var prior=db.queryForList("SELECT * FROM tickets WHERE tenant_id=? AND request_id=?",user.tenantId(),requestId);
            if(!prior.isEmpty()) {
                var row=prior.getFirst();
                if(!row.get("session_id").equals(sid)||!row.get("title").equals(title)) throw new ApiError(409,"幂等键已用于不同工单内容");
                return detail(user,(String)row.get("id"));
            }
            var sessions=db.queryForList("SELECT * FROM sessions WHERE id=?",sid);
            if(sessions.isEmpty()) throw new ApiError(404,"记录不存在或无权访问");
            identity.authorize(user,(String)sessions.getFirst().get("tenant_id"));
            var runs=db.queryForList("SELECT id,answer,evidence FROM runs WHERE session_id=? AND status IN ('completed','waiting_user') ORDER BY ended_at DESC LIMIT 1",sid);
            if(runs.isEmpty()) throw new ApiError(409,"请先完成一次调查，再创建含证据的工单");
            String id=SupportService.id("ticket");
            db.update("INSERT INTO tickets(id,tenant_id,session_id,created_by,request_id,title,evidence) VALUES (?,?,?,?,?,?,?::jsonb)",
                id,user.tenantId(),sid,user.id(),requestId,title,runs.getFirst().get("evidence").toString());
            String summary="从调查 "+runs.getFirst().get("id")+" 创建："+runs.getFirst().get("answer");
            db.update("INSERT INTO ticket_history(ticket_id,actor_id,to_status,note) VALUES (?,?,'OPEN',?)",id,user.id(),summary.substring(0,Math.min(summary.length(),2000)));
            return detail(user,id);
        });
    }
    public Map<String,Object> transition(Identity.User user,String id,int version,String target,String note) {
        return tx.execute(status->{
            var row=ticket(user,id,true);
            if(((Number)row.get("version")).intValue()!=version) throw new ApiError(409,"工单已被更新，请刷新后重试");
            String before=(String)row.get("status");
            boolean valid= switch(before) {
                case "OPEN" -> target.equals("ASSIGNED");
                case "ASSIGNED" -> target.equals("IN_PROGRESS");
                case "IN_PROGRESS" -> target.equals("RESOLVED");
                case "RESOLVED" -> target.equals("CLOSED")||target.equals("OPEN");
                default -> false;
            };
            if(!valid) throw new ApiError(409,"不允许的工单状态转换");
            if(before.equals("RESOLVED")) {
                if(!user.role().equals("MERCHANT")) throw new ApiError(403,"请商户确认关闭或重新打开");
            } else {
                if(!user.role().equals("SUPPORT")) throw new ApiError(403,"此操作需要技术支持角色");
                if(!before.equals("OPEN")&&!user.id().equals(row.get("assignee_id"))) throw new ApiError(403,"仅当前处理人可推进工单");
            }
            Object assignee=target.equals("OPEN")?null:before.equals("OPEN")?user.id():row.get("assignee_id");
            db.update("UPDATE tickets SET status=?,assignee_id=?,version=version+1 WHERE id=? AND version=?",target,assignee,id,version);
            // 审计与状态同一事务；审计失败必须回滚前面的状态更新。
            db.update("INSERT INTO ticket_history(ticket_id,actor_id,from_status,to_status,note) VALUES (?,?,?,?,?)",id,user.id(),before,target,note);
            return detail(user,id);
        });
    }
}
