/*
 * DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS HEADER.
 *
 * Copyright (c) [2016-2018] Payara Foundation and/or its affiliates. All rights reserved.
 *
 * The contents of this file are subject to the terms of either the GNU
 * General Public License Version 2 only ("GPL") or the Common Development
 * and Distribution License("CDDL") (collectively, the "License").  You
 * may not use this file except in compliance with the License.  You can
 * obtain a copy of the License at
 * https://github.com/payara/Payara/blob/master/LICENSE.txt
 * See the License for the specific
 * language governing permissions and limitations under the License.
 *
 * When distributing the software, include this License Header Notice in each
 * file and include the License file at glassfish/legal/LICENSE.txt.
 *
 * GPL Classpath Exception:
 * The Payara Foundation designates this particular file as subject to the "Classpath"
 * exception as provided by the Payara Foundation in the GPL Version 2 section of the License
 * file that accompanied this code.
 *
 * Modifications:
 * If applicable, add the following below the License Header, with the fields
 * enclosed by brackets [] replaced by your own identifying information:
 * "Portions Copyright [year] [name of copyright owner]"
 *
 * Contributor(s):
 * If you wish your version of this file to be governed by only the CDDL or
 * only the GPL Version 2, indicate your decision by adding "[Contributor]
 * elects to include this software in this distribution under the [CDDL or GPL
 * Version 2] license."  If you don't indicate a single choice of license, a
 * recipient has the option to distribute your version of this file under
 * either the CDDL, the GPL Version 2 or to extend the choice of license to
 * its licensees as provided above.  However, if you add GPL Version 2 code
 * and therefore, elected the GPL Version 2 license, then the option applies
 * only if the new code is made subject to such option by the copyright
 * holder.
 */
package fish.payara.micro.cdi.extension.cluster;

import com.sun.enterprise.deployment.EjbBundleDescriptor;
import fish.payara.cluster.Clustered;
import fish.payara.micro.cdi.extension.cluster.annotations.ClusterScoped;
import java.io.Serializable;
import javax.enterprise.context.ApplicationScoped;
import javax.enterprise.inject.spi.AfterBeanDiscovery;
import javax.enterprise.inject.spi.AnnotatedType;
import javax.enterprise.inject.spi.BeanManager;
import javax.enterprise.inject.spi.BeforeBeanDiscovery;
import javax.enterprise.inject.spi.ProcessAnnotatedType;
import lombok.extern.java.Log;
import org.glassfish.ejb.deployment.descriptor.EjbBundleDescriptorImpl;
import org.glassfish.internal.api.Globals;
import org.glassfish.internal.deployment.Deployment;

/**
 * Entry point for @Clustered annotation processing
 * for clustered @ApplicationScoped beans
 *
 * @author lprimak
 */
@Log
public class ClusteredAnnotationProcessor {
    private Deployment deployment;


    public void beforeBeanDiscovery(BeforeBeanDiscovery event, BeanManager beanManager) {
        event.addScope(ClusterScoped.class, true, true);
        deployment = Globals.getDefaultHabitat().getService(Deployment.class);
        event.addAnnotatedType(beanManager.createAnnotatedType(ClusterScopedInterceptor.class), ClusterScopedInterceptor.class.getName());
    }

    public void afterBeanDiscovery(AfterBeanDiscovery event, BeanManager manager) {
        event.addContext(new ClusterScopeContext(manager, deployment));
    }

    public <X> void processAnnotatedType(ProcessAnnotatedType<X> annotatedType, BeanManager beanManager) {
        Clustered clusteredAnnotation = annotatedType.getAnnotatedType().getAnnotation(Clustered.class);
        if (clusteredAnnotation != null && !isEJB(annotatedType)) {
            validate(annotatedType.getAnnotatedType(), beanManager);
            annotatedType.setAnnotatedType(new ClusteredAnnotatedType<>(annotatedType.getAnnotatedType()));
        }
    }

    private <X> boolean isEJB(ProcessAnnotatedType<X> annotatedType) {
        EjbBundleDescriptor bundleDescriptor = deployment.getCurrentDeploymentContext().getModuleMetaData(EjbBundleDescriptorImpl.class);
        return bundleDescriptor != null && bundleDescriptor.getEjbByClassName(annotatedType.getAnnotatedType().getJavaClass().getName()).length > 0;
    }

    private <X> void validate(AnnotatedType<X> annotatedType, BeanManager beanManager) {
        if (annotatedType.isAnnotationPresent(ApplicationScoped.class)) { }
        else {
            throw new IllegalArgumentException("Only @ApplicationScoped beans can be @Clustered: " + annotatedType.toString());
        }
        if (!Serializable.class.isAssignableFrom(annotatedType.getJavaClass())) {
            throw new IllegalStateException(String.format("Clustered @ApplicationScoped %s must be Serializable", annotatedType.toString()));
        }
    }
}