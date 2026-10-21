package dev.flowpay.support;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.HexFormat;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;

@Service
public class Identity {
    public record User(String id, String username, String tenantId, String role) {}
    private final JdbcTemplate db;
    private final SingleInstanceLease lease;
    private final TrafficPolicy traffic;
    private final BCryptPasswordEncoder passwords = new BCryptPasswordEncoder(12);
    private final SecureRandom random = new SecureRandom();
    private final String dummyHash = passwords.encode("invalid-login-placeholder");
    public Identity(JdbcTemplate db,SingleInstanceLease lease,TrafficPolicy traffic) { this.db = db; this.lease=lease; this.traffic=traffic; }

    public String hashPassword(String password) { return passwords.encode(password); }
    public void checkLease() { lease.check(); }
    public String login(String username, String password) {
        lease.check();
        traffic.window("login:"+digest(username),10,60);
        traffic.window("login:global",60,60);
        var rows = db.queryForList("SELECT * FROM app_users WHERE username=? AND active=true", username);
        String hash = rows.isEmpty() ? dummyHash : (String) rows.getFirst().get("password_hash");
        if (!passwords.matches(password, hash) || rows.isEmpty()) throw new ApiError(401, "账号或密码错误");
        byte[] bytes = new byte[32]; random.nextBytes(bytes);
        String token = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
        db.update("DELETE FROM auth_tokens WHERE expires_at < now()");
        db.update("INSERT INTO auth_tokens VALUES (?,?,now()+interval '8 hours')", digest(token), rows.getFirst().get("id"));
        return token;
    }
    public User authenticate(String token) {
        lease.check();
        if (token == null || token.length() > 100) throw new ApiError(401, "请先登录");
        var users = db.query("SELECT u.* FROM auth_tokens t JOIN app_users u ON u.id=t.user_id "
            + "WHERE t.token_hash=? AND t.expires_at>now() AND u.active=true", (r,n) ->
                new User(r.getString("id"), r.getString("username"), r.getString("tenant_id"), r.getString("role")), digest(token));
        if (users.isEmpty()) throw new ApiError(401, "登录已失效");
        return users.getFirst();
    }
    public void logout(String token) { db.update("DELETE FROM auth_tokens WHERE token_hash=?", digest(token)); }
    public void authorize(User user, String tenant) {
        if(!Boolean.TRUE.equals(db.queryForObject("SELECT active FROM app_users WHERE id=?",Boolean.class,user.id())))
            throw new ApiError(403,"账号已禁用");
        if (user.tenantId().equals(tenant) && user.role().equals("MERCHANT")) return;
        Integer count = db.queryForObject("SELECT count(*) FROM support_grants WHERE user_id=? AND tenant_id=?", Integer.class, user.id(), tenant);
        if (user.role().equals("SUPPORT") && count != null && count > 0) return;
        throw new ApiError(404, "记录不存在或无权访问");
    }
    public java.util.List<String> permissions(User user) {
        return db.queryForList("SELECT permission FROM user_permissions WHERE user_id=?",String.class,user.id());
    }
    public void require(User user,String permission) {
        if(!permissions(user).contains(permission)) throw new ApiError(403,"缺少权限："+permission);
    }
    private static String digest(String value) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8))); }
        catch (Exception e) { throw new IllegalStateException(e); }
    }
}
