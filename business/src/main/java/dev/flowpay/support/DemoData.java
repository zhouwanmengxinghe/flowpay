package dev.flowpay.support;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

@Component
public class DemoData implements ApplicationRunner {
    private final JdbcTemplate db; private final Identity identity; private final boolean demo; private final String password;
    public DemoData(JdbcTemplate db, Identity identity, @Value("${support.demo}") boolean demo,
                    @Value("${support.demo-password}") String password) {
        this.db=db; this.identity=identity; this.demo=demo; this.password=password;
    }
    @Override @Transactional
    public void run(ApplicationArguments args) {
        if (!demo) return;
        if (password.length()<12) throw new IllegalStateException("Demo password must contain at least 12 characters");
        db.update("INSERT INTO tenants VALUES ('merchant-a','示例商户 A'),('merchant-b','示例商户 B') ON CONFLICT DO NOTHING");
        db.update("INSERT INTO tenant_accounts VALUES ('merchant-a','acct_bg_001'),('merchant-b','acct_bm_030') ON CONFLICT DO NOTHING");
        db.update("INSERT INTO tenant_limits(tenant_id) SELECT id FROM tenants ON CONFLICT DO NOTHING");
        create("merchant-a", "merchant_a", "merchant-a", "MERCHANT");
        create("merchant-b", "merchant_b", "merchant-b", "MERCHANT");
        create("support-a", "support_a", "merchant-a", "SUPPORT");
        create("support-b", "support_b", "merchant-a", "SUPPORT");
        db.update("INSERT INTO support_grants VALUES ('support-a','merchant-a'),('support-b','merchant-a') ON CONFLICT DO NOTHING");
        db.update("INSERT INTO user_permissions VALUES ('merchant-a','MERCHANT_ADMIN'),('merchant-b','MERCHANT_ADMIN'),('support-a','SUPPORT_ADMIN'),('support-a','OBSERVE') ON CONFLICT DO NOTHING");
    }
    private void create(String id, String username, String tenant, String role) {
        Integer exists = db.queryForObject("SELECT count(*) FROM app_users WHERE id=?", Integer.class,id);
        if (exists != null && exists == 0) db.update("INSERT INTO app_users(id,username,password_hash,tenant_id,role) VALUES (?,?,?,?,?)",
            id,username,identity.hashPassword(password),tenant,role);
    }
}
