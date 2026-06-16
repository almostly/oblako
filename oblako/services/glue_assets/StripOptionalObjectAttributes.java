package io.oblako.glue;

import software.amazon.awssdk.core.interceptor.Context;
import software.amazon.awssdk.core.interceptor.ExecutionAttributes;
import software.amazon.awssdk.core.interceptor.ExecutionInterceptor;
import software.amazon.awssdk.http.SdkHttpRequest;

/**
 * Drops the {@code x-amz-optional-object-attributes} header that Glue's bundled
 * AWS SDK adds to listObjectsV2 (the RestoreStatus / Glacier optimization).
 * S3Proxy 501s on it ("A header you provided implies functionality that is not
 * implemented"); restore-status is meaningless for oblako's local S3, so
 * removing it is harmless. Runs before signing, so the signature stays valid.
 *
 * Registered by GlueService via
 * {@code spark.hadoop.fs.s3a.audit.execution.interceptors}.
 */
public class StripOptionalObjectAttributes implements ExecutionInterceptor {
    @Override
    public SdkHttpRequest modifyHttpRequest(
            Context.ModifyHttpRequest context, ExecutionAttributes executionAttributes) {
        return context.httpRequest().toBuilder()
                .removeHeader("x-amz-optional-object-attributes")
                .build();
    }
}
