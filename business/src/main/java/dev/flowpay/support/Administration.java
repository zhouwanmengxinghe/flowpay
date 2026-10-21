package dev.flowpay.support;

import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

/** 管理操作始终重查权限；商户管理员无法创建平台人员或指定其他商户。 */
@Service
public class Administration {
    private final JdbcTemplate db; private final Identity identity; private final TransactionTemplate tx; private final Json json;
    private final RunCoordinator coordinator;
    public Administration(JdbcTemplate db,Identity identity,TransactionTemplate tx,Json json,RunCoordinator coordinator) {
        this.db=db; this.identity=identity; this.tx=tx; this.json=json; this.coordinator=coordinator;
    }
    private void merchantAdmin(Identity.User user) {
        if(!user.role().equals("MERCHANT")) throw new ApiError(403,"仅商户管理员可管理成员");
        identity.require(user,"MERCHANT_ADMIN");
    }
    private void audit(Identity.User user,String tenant,String action,String target,Object payload) {
        db.update("INSERT INTO business_audit(actor_id,tenant_id,action,target_id,payload) VALUES (?,?,?,?,?::jsonb)",user.id(),tenant,action,target,json.write(payload));
    }
    public List<Map<String,Object>> members(Identity.User user) {
        merchantAdmin(user);
        return db.queryForList("SELECT u.id,u.username,u.active,EXISTS(SELECT 1 FROM user_permissions p WHERE p.user_id=u.id AND permission='MERCHANT_ADMIN') AS administrator FROM app_users u WHERE tenant_id=? AND role='MERCHANT' ORDER BY username",user.tenantId());
    }
    public Object createMember(Identity.User user,String username,String password,boolean admin) {
        merchantAdmin(user);
        if(password.getBytes(java.nio.charset.StandardCharsets.UTF_8).length>72) throw new ApiError(400,"密码不能超过 72 UTF-8 字节");
        String hash=identity.hashPassword(password);
        return tx.execute(status->{
            db.queryForList("SELECT id FROM tenants WHERE id=? FOR UPDATE",user.tenantId());
            merchantAdmin(user);
            if(!db.queryForList("SELECT id FROM app_users WHERE username=?",username).isEmpty()) throw new ApiError(409,"用户名已存在");
            String id=SupportService.id("user");
            db.update("INSERT INTO app_users(id,username,password_hash,tenant_id,role) VALUES (?,?,?,?,'MERCHANT')",id,username,hash,user.tenantId());
            if(admin) db.update("INSERT INTO user_permissions VALUES (?,'MERCHANT_ADMIN')",id);
            audit(user,user.tenantId(),"member.created",id,Map.of("administrator",admin));
            return Map.of("id",id,"username",username,"administrator",admin);
        });
    }
    public void updateMember(Identity.User user,String id,boolean active,boolean admin) {
        merchantAdmin(user);
        tx.executeWithoutResult(status->{
            db.queryForList("SELECT id FROM tenants WHERE id=? FOR UPDATE",user.tenantId());
            merchantAdmin(user);
            var members=db.queryForList("SELECT id FROM app_users WHERE id=? AND tenant_id=? AND role='MERCHANT'",id,user.tenantId());
            if(members.isEmpty()) throw new ApiError(404,"成员不存在");
            if((!active||!admin)&&db.queryForObject("SELECT EXISTS(SELECT 1 FROM user_permissions WHERE user_id=? AND permission='MERCHANT_ADMIN')",Boolean.class,id)) {
                Long remaining=db.queryForObject("SELECT count(*) FROM app_users u JOIN user_permissions p ON u.id=p.user_id WHERE tenant_id=? AND active=true AND permission='MERCHANT_ADMIN' AND u.id<>?",Long.class,user.tenantId(),id);
                if(remaining==0) throw new ApiError(409,"不能禁用或降级最后一个管理员");
            }
            db.update("UPDATE app_users SET active=? WHERE id=?",active,id);
            db.update("DELETE FROM user_permissions WHERE user_id=? AND permission='MERCHANT_ADMIN'",id);
            if(admin) db.update("INSERT INTO user_permissions VALUES (?,'MERCHANT_ADMIN')",id);
            if(!active) {
                db.update("DELETE FROM auth_tokens WHERE user_id=?",id);
                for(String sid:db.queryForList("SELECT id FROM sessions WHERE owner_id=? AND status IN ('queued','running') ORDER BY id",String.class,id))
                    coordinator.cancelSession(sid);
            }
            audit(user,user.tenantId(),"member.updated",id,Map.of("active",active,"administrator",admin));
        });
    }
    public Object grants(Identity.User user) {
        identity.require(user,"SUPPORT_ADMIN");
        return Map.of("grants",db.queryForList("SELECT * FROM support_grants ORDER BY user_id,tenant_id"),
            "supportUsers",db.queryForList("SELECT id,username FROM app_users WHERE role='SUPPORT' AND active=true"),
            "tenants",db.queryForList("SELECT id,name FROM tenants"));
    }
    public void grant(Identity.User user,String support,String tenant,boolean enabled) {
        identity.require(user,"SUPPORT_ADMIN");
        tx.executeWithoutResult(status->{
            db.queryForList("SELECT id FROM app_users WHERE id=? FOR UPDATE",support);
            if(db.queryForList("SELECT id FROM app_users WHERE id=? AND role='SUPPORT' AND active=true",support).isEmpty()||db.queryForList("SELECT id FROM tenants WHERE id=?",tenant).isEmpty()) throw new ApiError(404,"支持人员或商户不存在");
            if(enabled) db.update("INSERT INTO support_grants VALUES (?,?) ON CONFLICT DO NOTHING",support,tenant);
            else db.update("DELETE FROM support_grants WHERE user_id=? AND tenant_id=?",support,tenant);
            audit(user,tenant,enabled?"grant.created":"grant.revoked",support,Map.of());
        });
    }
    public Object audit(Identity.User user) {
        merchantAdmin(user);
        return db.queryForList("SELECT id,actor_id,action,target_id,payload,created_at FROM business_audit WHERE tenant_id=? ORDER BY id DESC LIMIT 100",user.tenantId());
    }
    public void limits(Identity.User user,String tenant,int rpm,int daily,int active) {
        identity.require(user,"SUPPORT_ADMIN");
        tx.executeWithoutResult(status->{
            if(db.queryForList("SELECT id FROM tenants WHERE id=? FOR UPDATE",tenant).isEmpty()) throw new ApiError(404,"商户不存在");
            db.update("UPDATE tenant_limits SET requests_per_minute=?,daily_runs=?,active_runs=? WHERE tenant_id=?",rpm,daily,active,tenant);
            audit(user,tenant,"limits.updated",tenant,Map.of("requestsPerMinute",rpm,"dailyRuns",daily,"activeRuns",active));
        });
    }
}
